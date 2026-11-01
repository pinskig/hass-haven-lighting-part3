"""Offline transport regressions through real Requests session/redirect logic.

Only HTTPAdapter.send is replaced; redirects, prepared requests, cookies and
session reuse still execute Requests' real code. No socket or Haven is used.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "haven"))

from havenlighting.client import HavenClient
from havenlighting.config import ApiConfig
from havenlighting import credentials as credentials_module
from havenlighting.credentials import Credentials
from havenlighting.exceptions import ApiError
from havenlighting.logging import setup_logging
from havenlighting import request_gate as request_gate_module
from havenlighting.request_gate import RequestGate


class Clock:
    def __init__(self):
        self.monotonic_now = 1000.0
        self.wall_now = 1800000000.0

    def monotonic(self):
        return self.monotonic_now

    def time(self):
        return self.wall_now

    def sleep(self, delay):
        self.advance(delay)

    def advance(self, delay):
        self.monotonic_now += delay
        self.wall_now += delay


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for module in (credentials_module, request_gate_module):
            timer = patch.object(module, "time", self.clock)
            timer.start()
            self.addCleanup(timer.stop)
        self.replies = []
        self.sent = []

        def send(adapter, request, **kwargs):
            self.sent.append((adapter, request, kwargs, self.clock.monotonic()))
            if not self.replies:
                raise AssertionError("Unexpected HTTP request in offline transport test")
            status, data, headers = self.replies.pop(0)
            return self.response(request, status, data, headers)

        network = patch.object(requests.adapters.HTTPAdapter, "send", new=send)
        network.start()
        self.addCleanup(network.stop)

    @staticmethod
    def response(request, status=200, data=None, headers=None):
        result = requests.Response()
        result.request = request
        result.url = request.url
        result.status_code = status
        result.headers.update(headers or {})
        result._content = json.dumps({} if data is None else data).encode()
        result._content_consumed = True
        result.encoding = "utf-8"
        return result

    def credentials(self, gate=None, config=None):
        credentials = Credentials(request_gate=gate, api_config=config)
        # Isolate tests from host proxy/netrc/CA configuration.
        credentials._session.trust_env = False
        credentials._update_credentials(
            {"token": "audit-token", "refreshToken": "audit-refresh", "id": 123}
        )
        self.addCleanup(credentials.close)
        return credentials

    def test_redirects_never_forward_login_credentials(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                credentials = self.credentials(RequestGate(min_request_interval=0))
                self.replies.append(
                    (status, {}, {"Location": "https://untrusted.example.invalid/login"})
                )
                before = len(self.sent)
                with self.assertRaises(ApiError) as raised:
                    credentials.authenticate("audit@example.invalid", "DUMMY_PASSWORD")
                self.assertEqual(raised.exception.code, status)
                self.assertEqual(len(self.sent), before + 1)
                original = self.sent[-1][1]
                self.assertTrue(original.url.startswith("https://stg-api.havenlighting.com/"))
                self.assertIn(b"DUMMY_PASSWORD", original.body)
                self.assertNotIn("Authorization", original.headers)

    def test_same_session_and_adapter_are_reused_and_closed_once(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.extend([(200, {"first": True}, {}), (200, {"second": True}, {})])
        session = credentials._session
        credentials.make_request("GET", "/first")
        credentials.make_request("GET", "/second")
        self.assertIs(credentials._session, session)
        self.assertIs(self.sent[0][0], self.sent[1][0])
        with patch.object(session, "close", wraps=session.close) as close:
            credentials.close()
            credentials.close()
            self.assertEqual(close.call_count, 1)
        self.assertTrue(credentials.closed)
        with self.assertRaises(ApiError):
            credentials.make_request("GET", "/after-close")
        self.assertEqual(len(self.sent), 2)

    def test_shared_gate_survives_client_replacement_and_blocks_every_path(self):
        gate = RequestGate()
        first = self.credentials(gate)
        self.replies.append((429, {}, {"Retry-After": "3600"}))
        with self.assertRaises(ApiError):
            first.make_request("GET", "/poll")
        first.close()

        replacement = self.credentials(gate)
        operations = (
            lambda: replacement.authenticate("audit@example.invalid", "DUMMY"),
            replacement.refresh_token,
            lambda: replacement.make_request("GET", "/poll"),
            lambda: replacement.make_request("POST", "/Commands/On", json={}),
        )
        for operation in operations:
            with self.assertRaises(ApiError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 429)
            self.assertEqual(raised.exception.retry_after, 3600)
            self.assertTrue(raised.exception.rate_limited_locally)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(gate.snapshot()["consecutive_429"], 1)

    def test_request_pacing_is_shared_between_sessions(self):
        gate = RequestGate(min_request_interval=1.0)
        first = self.credentials(gate)
        second = self.credentials(gate)
        self.replies.extend([(200, {}, {}), (200, {}, {})])
        first.make_request("GET", "/first")
        second.make_request("GET", "/second")
        self.assertGreaterEqual(self.sent[1][3] - self.sent[0][3], 1.0)
        self.assertIsNot(self.sent[0][0], self.sent[1][0])

    def test_503_honors_retry_after_without_escalating_429(self):
        gate = RequestGate(min_request_interval=0)
        credentials = self.credentials(gate)
        self.replies.append((429, {}, {}))
        with self.assertRaises(ApiError):
            credentials.make_request("GET", "/poll")
        self.clock.advance(60)
        self.replies.append((503, {}, {"Retry-After": "1200"}))
        with self.assertRaises(ApiError) as raised:
            credentials.make_request("GET", "/poll")
        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(raised.exception.retry_after, 1200)
        self.assertEqual(gate.snapshot()["consecutive_429"], 1)
        with self.assertRaises(ApiError) as local:
            self.credentials(gate).make_request("POST", "/Commands/Off", json={})
        self.assertEqual(local.exception.code, 503)
        self.assertTrue(local.exception.rate_limited_locally)
        self.assertEqual(len(self.sent), 2)

    def test_503_without_retry_after_remains_an_ordinary_failure(self):
        gate = RequestGate(min_request_interval=0)
        credentials = self.credentials(gate)
        self.replies.append((503, {}, {}))
        with self.assertRaises(ApiError) as raised:
            credentials.make_request("GET", "/poll")
        self.assertEqual(raised.exception.code, 503)
        self.assertIsNone(raised.exception.retry_after)
        self.assertEqual(gate.rate_limit_remaining, 0)

    def test_snapshot_restores_deadline_across_monotonic_clock_restart(self):
        gate = RequestGate()
        credentials = self.credentials(gate)
        self.replies.append((429, {}, {"Retry-After": "3600"}))
        with self.assertRaises(ApiError):
            credentials.make_request("GET", "/poll")
        saved = json.loads(json.dumps(gate.snapshot()))

        self.clock.wall_now += 600
        self.clock.monotonic_now = 50.0
        restored = RequestGate()
        self.assertTrue(restored.restore(saved))
        self.assertEqual(restored.rate_limit_remaining, 3000)
        self.assertEqual(restored.snapshot()["retry_at"], saved["retry_at"])
        with self.assertRaises(ApiError):
            self.credentials(restored).make_request("GET", "/poll")
        self.assertEqual(len(self.sent), 1)

    def test_restore_cannot_shorten_active_cooldown_or_accept_invalid_state(self):
        gate = RequestGate()
        gate.record_rate_limit(3600)
        old = gate.snapshot()
        invalid = (
            {"retry_at": float("nan"), "status": 429},
            {"retry_at": float("inf"), "status": 429},
            {"retry_at": -1, "status": 429},
            {"retry_at": RequestGate.MAX_RETRY_AT + 1, "status": 429},
            {"retry_at": 10 ** 4000, "status": 429},
            {"retry_at": old["retry_at"], "status": 200},
            {"retry_at": old["retry_at"], "status": 429, "consecutive_429": -1},
            {"retry_at": "tomorrow", "status": 429},
        )
        for state in invalid:
            self.assertFalse(gate.restore(state))
            self.assertEqual(gate.rate_limit_remaining, 3600)
        self.assertTrue(gate.restore({"retry_at": None, "status": None, "consecutive_429": 0}))
        self.assertEqual(gate.rate_limit_remaining, 3600)
        self.assertEqual(gate.snapshot()["consecutive_429"], 1)

    def test_on_change_records_server_deadlines_not_local_rejections(self):
        changes = []
        gate = RequestGate(on_change=changes.append)
        credentials = self.credentials(gate)
        self.replies.append((429, {}, {"Retry-After": "600"}))
        for _ in range(3):
            with self.assertRaises(ApiError):
                credentials.make_request("GET", "/poll")
        self.assertEqual(len(changes), 1)
        self.assertEqual(set(changes[0]), {"retry_at", "status", "consecutive_429"})
        self.assertEqual(changes[0]["status"], 429)
        self.assertEqual(changes[0]["retry_at"], self.clock.wall_now + 600)

    def test_api_config_is_the_only_endpoint_and_timeout_source(self):
        config = ApiConfig(
            auth_base_url="https://auth.example.invalid/api/",
            data_base_url="https://data.example.invalid/v2/",
            connect_timeout=2,
            read_timeout=12,
        )
        credentials = self.credentials(RequestGate(min_request_interval=0), config)
        self.replies.extend([
            (200, {"token": "new-token", "refreshToken": "new-refresh", "id": 123}, {}),
            (200, {}, {}),
        ])
        credentials.authenticate("audit@example.invalid", "DUMMY")
        credentials.make_request("GET", "/poll", use_prod_api=True)
        self.assertEqual(self.sent[0][1].url, "https://auth.example.invalid/api/Auth/Authenticate")
        self.assertEqual(self.sent[1][1].url, "https://data.example.invalid/v2/poll")
        self.assertEqual(self.sent[0][2]["timeout"], (2, 12))
        self.assertEqual(self.sent[1][2]["timeout"], (2, 12))
        with self.assertRaises(ValueError):
            ApiConfig(auth_base_url="http://unsafe.example.invalid/api")

    def test_account_id_is_the_api_id_and_not_the_email(self):
        client = HavenClient()
        self.addCleanup(client.close)
        client._credentials._session.trust_env = False
        self.replies.append(
            (200, {"token": "t", "refreshToken": "r", "id": 98765}, {})
        )
        client.authenticate("audit@example.invalid", "DUMMY")
        self.assertEqual(client.account_id, "98765")

    def test_auth_header_is_not_replaced_by_netrc(self):
        credentials = self.credentials()
        credentials._session.trust_env = True
        self.replies.append((200, {}, {}))
        with patch.object(
            requests.sessions, "get_netrc_auth", return_value=("ambient-user", "ambient-password")
        ) as netrc:
            credentials.make_request("GET", "/poll")
        self.assertEqual(self.sent[0][1].headers["Authorization"], "Bearer audit-token")
        netrc.assert_not_called()

    def test_clients_do_not_change_logging_handlers(self):
        package_logger = logging.getLogger("havenlighting")
        before = list(package_logger.handlers)
        clients = [HavenClient() for _ in range(3)]
        for client in clients:
            self.addCleanup(client.close)
        self.assertEqual(package_logger.handlers, before)

    def test_explicit_standalone_logging_setup_is_idempotent(self):
        package_logger = logging.getLogger("havenlighting")
        before = list(package_logger.handlers)
        old_level = package_logger.level
        try:
            setup_logging()
            setup_logging()
            owned = [h for h in package_logger.handlers if getattr(h, "_haven_owned_handler", False)]
            self.assertEqual(len(owned), 1)
            for handler in before:
                self.assertIn(handler, package_logger.handlers)
        finally:
            for handler in list(package_logger.handlers):
                if handler not in before:
                    package_logger.removeHandler(handler)
                    handler.close()
            package_logger.setLevel(old_level)

    def test_distinct_clients_cannot_overlap_physical_requests(self):
        gate = RequestGate(min_request_interval=0)
        attempted = set()
        attempt_lock = threading.Lock()
        both_attempting = threading.Event()
        inner_lock = threading.RLock()

        class ObservedLock:
            def __enter__(self):
                with attempt_lock:
                    attempted.add(threading.get_ident())
                    if len(attempted) >= 2:
                        both_attempting.set()
                inner_lock.acquire()
                return self

            def __exit__(self, *args):
                inner_lock.release()

        gate.lock = ObservedLock()
        first, second = self.credentials(gate), self.credentials(gate)
        entered = threading.Event()
        release = threading.Event()
        active = 0
        maximum = 0
        sends = 0

        def send(adapter, request, **kwargs):
            nonlocal active, maximum, sends
            active += 1
            maximum = max(maximum, active)
            sends += 1
            if sends == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release first request")
            active -= 1
            return self.response(request)

        with patch.object(requests.adapters.HTTPAdapter, "send", new=send):
            with ThreadPoolExecutor(max_workers=2) as executor:
                one = executor.submit(first.make_request, "GET", "/first")
                self.assertTrue(entered.wait(3))
                two = executor.submit(second.make_request, "GET", "/second")
                try:
                    self.assertTrue(both_attempting.wait(3))
                finally:
                    release.set()
                one.result(timeout=3)
                two.result(timeout=3)
        self.assertEqual(sends, 2)
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
