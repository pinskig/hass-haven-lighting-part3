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
from havenlighting.devices.light import Light
from havenlighting.exceptions import ApiError, AuthenticationError
from havenlighting.logging import setup_logging
from havenlighting.models import LightState, TargetKey
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
        # Bytes model the wire body exactly, including a zero-byte response.
        result._content = data if isinstance(data, bytes) else json.dumps({} if data is None else data).encode()
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

    @staticmethod
    def light(credentials, *, is_on=False):
        return Light.from_state(credentials, LightState(
            TargetKey(123456, "Zone", 101), "Offline test zone", is_on, 128, 11,
        ))

    def test_empty_successful_commands_publish_state_once_and_close_response(self):
        commands = (
            ("turn_on", (), {"is_on": True}),
            ("turn_off", (), {"is_on": False}),
            ("set_brightness", (8,), {"is_on": True, "brightness": 204}),
            ("set_color", (25,), {"color_id": 25}),
        )
        for status in (200, 202, 204):
            for name, arguments, expected in commands:
                with self.subTest(status=status, command=name):
                    credentials = self.credentials(RequestGate(min_request_interval=0))
                    light = self.light(credentials, is_on=name == "turn_off")
                    self.replies.append((status, b"", {}))
                    before = len(self.sent)
                    with patch.object(requests.Response, "close", autospec=True,
                                      side_effect=requests.Response.close) as close:
                        getattr(light, name)(*arguments)
                    self.assertEqual(len(self.sent), before + 1)
                    close.assert_called_once()
                    self.assertIs(close.call_args.args[0].request, self.sent[-1][1])
                    for field, value in expected.items():
                        self.assertEqual(getattr(light.state, field), value)

    def test_command_json_response_remains_compatible(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        light = self.light(credentials)
        self.replies.append((200, {"accepted": True}, {}))
        light.set_color(25)
        self.assertEqual(light.color_id, 25)
        self.assertEqual(len(self.sent), 1)

    def test_empty_response_remains_an_error_without_command_opt_in(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        operations = (
            ("authenticate", lambda: credentials.authenticate("offline@example.invalid", "DUMMY_PASSWORD")),
            ("refresh", credentials.refresh_token),
            ("discovery", lambda: credentials.make_request("GET", "/user/GetUserInfo")),
            ("zones", lambda: credentials.make_request("GET", "/LightAndZones/OrderedList/123456")),
            ("groups", lambda: credentials.make_request("GET", "/Group/AllGroupsByLocation/123456")),
            ("no opt-in", lambda: credentials.make_request("POST", "/Commands/SetColor", json={})),
        )
        for name, operation in operations:
            with self.subTest(operation=name):
                self.replies.append((200, b"", {}))
                before = len(self.sent)
                with self.assertRaises(ApiError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, 200)
                self.assertIn("empty response", str(raised.exception))
                self.assertEqual(len(self.sent), before + 1)
                self.assertEqual(credentials._token, "audit-token")

    def test_nonempty_invalid_command_json_never_publishes_or_replays(self):
        for status in (200, 202):
            for body in (b"<html>PRIVATE_BODY</html>", b"{broken", b" \r\n"):
                with self.subTest(status=status, body=body):
                    credentials = self.credentials(RequestGate(min_request_interval=0))
                    light = self.light(credentials)
                    previous = light.state
                    self.replies.append((status, body, {}))
                    before = len(self.sent)
                    with patch.object(requests.Response, "close", autospec=True,
                                      side_effect=requests.Response.close) as close:
                        with self.assertRaises(ApiError) as raised:
                            light.set_color(25)
                    self.assertEqual(raised.exception.code, status)
                    self.assertIn("invalid JSON", str(raised.exception))
                    self.assertIn("/Commands/SetColor", str(raised.exception))
                    self.assertIn(f"HTTP {status}", str(raised.exception))
                    self.assertIs(light.state, previous)
                    self.assertEqual(len(self.sent), before + 1)
                    close.assert_called_once()

    def test_empty_command_opt_in_survives_401_refresh_and_retry(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        light = self.light(credentials)
        self.replies.extend([
            (401, b"", {}),
            (200, {"token": "replacement-token", "refreshToken": "replacement-refresh", "id": 123}, {}),
            (202, b"", {}),
        ])
        light.set_color(25)
        self.assertEqual(light.color_id, 25)
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(self.sent[0][1].url.endswith("/Commands/SetColor"))
        self.assertTrue(self.sent[1][1].url.endswith("/Auth/Refresh"))
        self.assertTrue(self.sent[2][1].url.endswith("/Commands/SetColor"))
        self.assertEqual(self.sent[0][1].body, self.sent[2][1].body)
        self.assertEqual(self.sent[2][1].headers["Authorization"], "Bearer replacement-token")

    def test_command_opt_in_never_applies_to_token_refresh(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        light = self.light(credentials)
        previous = light.state
        self.replies.extend([(401, b"", {}), (200, b"", {})])
        with self.assertRaises(ApiError) as raised:
            light.set_color(25)
        self.assertEqual(raised.exception.code, 200)
        self.assertIn("empty response", str(raised.exception))
        self.assertIn("/Auth/Refresh", str(raised.exception))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(credentials._token, "audit-token")
        self.assertIs(light.state, previous)

    def test_empty_command_errors_preserve_failure_and_cooldown(self):
        for status in (400, 429, 503, 500):
            with self.subTest(status=status):
                gate = RequestGate(min_request_interval=0)
                credentials = self.credentials(gate)
                light = self.light(credentials)
                previous = light.state
                limited = status in (429, 503)
                self.replies.append((status, b"", {"Retry-After": "3600"} if limited else {}))
                before = len(self.sent)
                with self.assertRaises(ApiError) as raised:
                    light.set_color(25)
                self.assertEqual(raised.exception.code, status)
                self.assertEqual(gate.rate_limit_remaining, 3600 if limited else 0)
                self.assertIs(light.state, previous)
                self.assertEqual(len(self.sent), before + 1)

    def test_json_decode_errors_omit_body_headers_ids_and_query_values(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.append((200, b"<html>PRIVATE_RESPONSE</html>", {"X-Private": "PRIVATE_HEADER"}))
        with self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(ApiError) as raised:
                credentials.make_request(
                    "POST", "/Commands/SetColor/987654?token=PRIVATE_QUERY",
                    json={"private": "PRIVATE_REQUEST"}, allow_empty_response=True,
                )
        self.assertEqual(raised.exception.code, 200)
        self.assertIn("/Commands/SetColor", str(raised.exception))
        self.assertIn("HTTP 200", str(raised.exception))
        diagnostics = "\n".join([str(raised.exception), *captured.output])
        for secret in ("PRIVATE_", "987654", "audit-token", "audit-refresh"):
            self.assertNotIn(secret, diagnostics)
        self.assertEqual(len(self.sent), 1)

    def test_authenticate_wire_contract_matches_inherited_payload(self):
        device_id = credentials_module.uuid.UUID("12345678-1234-4234-8234-123456789abc")
        with patch.object(credentials_module.uuid, "uuid4", return_value=device_id):
            credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.append((200, {
            "token": "replacement-token", "refreshToken": "replacement-refresh", "id": 123,
        }, {}))

        self.assertTrue(credentials.authenticate("offline@example.invalid", "DUMMY_PASSWORD"))

        self.assertEqual(len(self.sent), 1)
        request = self.sent[0][1]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, "https://stg-api.havenlighting.com/api/Auth/Authenticate")
        self.assertEqual(json.loads(request.body), {
            "Username": "offline@example.invalid",
            "Email": "offline@example.invalid",
            "Password": "DUMMY_PASSWORD",
            "DeviceId": str(device_id),
        })
        self.assertEqual(request.headers["User-Agent"], (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ))
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(int(request.headers["Content-Length"]), len(request.body))
        # The helper seeds an old bearer token; anonymous login must omit it.
        self.assertNotIn("Authorization", request.headers)

    def test_authenticate_400_requests_reauthentication_without_exposing_secrets(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.append((400, {"message": "PRIVATE_PROVIDER_RESPONSE"}, {}))
        closed = []
        original_close = requests.Response.close

        def close(response):
            closed.append(response)
            return original_close(response)

        with patch.object(requests.Response, "close", new=close), \
                self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(AuthenticationError) as raised:
                credentials.authenticate("private-email@example.invalid", "PRIVATE_PASSWORD")

        self.assertEqual(raised.exception.code, 400)
        self.assertIn("Haven rejected the sign-in request (HTTP 400)", str(raised.exception))
        self.assertIn("reauthenticate", str(raised.exception))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(closed), 1)
        self.assertIs(closed[0].request, self.sent[0][1])
        self.assertEqual(credentials._token, "audit-token")
        self.assertEqual(credentials.request_gate.rate_limit_remaining, 0)
        diagnostics = "\n".join([
            str(raised.exception), str(raised.exception.__cause__), *captured.output,
        ])
        for secret in ("PRIVATE_PROVIDER_RESPONSE", "PRIVATE_PASSWORD", "private-email@example.invalid", "audit-token"):
            self.assertNotIn(secret, diagnostics)

    def test_authenticate_classification_does_not_change_other_endpoint_400s(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        operations = (
            ("poll", lambda: credentials.make_request("GET", "/LightAndZones/OrderedList/123456")),
            ("command", lambda: credentials.make_request("POST", "/Commands/On", json={"id": 101})),
            ("token refresh", credentials.refresh_token),
        )
        for name, operation in operations:
            with self.subTest(operation=name):
                self.replies.append((400, {}, {}))
                before = len(self.sent)
                with self.assertRaises(ApiError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, 400)
                self.assertNotIsInstance(raised.exception, AuthenticationError)
                self.assertEqual(len(self.sent), before + 1)

    def test_authenticate_rate_limits_and_service_failures_remain_retryable(self):
        for status, retry_after, delay in ((429, "3600", 3600), (503, "3600", 3600), (503, None, 0)):
            with self.subTest(status=status, retry_after=retry_after):
                gate = RequestGate(min_request_interval=0)
                credentials = self.credentials(gate)
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                self.replies.append((status, {}, headers))
                before = len(self.sent)
                with self.assertRaises(ApiError) as raised:
                    credentials.authenticate("offline@example.invalid", "DUMMY_PASSWORD")
                self.assertEqual(raised.exception.code, status)
                self.assertEqual(raised.exception.retry_after, delay or None)
                self.assertEqual(gate.rate_limit_remaining, delay)
                self.assertEqual(gate.snapshot()["consecutive_429"], int(status == 429))
                self.assertEqual(len(self.sent), before + 1)
                if delay:
                    with self.assertRaises(ApiError) as blocked:
                        credentials.authenticate("offline@example.invalid", "DUMMY_PASSWORD")
                    self.assertTrue(blocked.exception.rate_limited_locally)
                    self.assertEqual(gate.rate_limit_remaining, delay)
                    self.assertEqual(len(self.sent), before + 1)

    def test_authenticate_timeout_remains_retryable(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))

        def send(adapter, request, **kwargs):
            self.sent.append((adapter, request, kwargs, self.clock.monotonic()))
            raise requests.exceptions.Timeout("offline timeout")

        with patch.object(requests.adapters.HTTPAdapter, "send", new=send):
            with self.assertRaises(ApiError) as raised:
                credentials.authenticate("offline@example.invalid", "DUMMY_PASSWORD")
        self.assertIsNone(raised.exception.code)
        self.assertIsNone(raised.exception.retry_after)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(credentials._token, "audit-token")
        self.assertEqual(credentials.request_gate.rate_limit_remaining, 0)

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

    def test_debug_counts_each_attempt_and_excludes_pacing_from_elapsed(self):
        credentials = self.credentials(RequestGate(min_request_interval=1))

        def send(adapter, request, **kwargs):
            self.sent.append((adapter, request, kwargs, self.clock.monotonic()))
            self.clock.advance(0.25)
            return self.response(request)

        with patch.object(requests.adapters.HTTPAdapter, "send", new=send), \
                self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            credentials.make_request("GET", "/LightAndZones/OrderedList/987654")
            credentials.make_request("GET", "/Group/AllGroupsByLocation/987654")

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1][3] - self.sent[0][3], 1)
        self.assertEqual([record.getMessage() for record in captured.records], [
            "Haven HTTP attempt: method=GET endpoint=/LightAndZones/OrderedList status=200 elapsed=0.250s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes=2",
            "Haven HTTP attempt: method=GET endpoint=/Group/AllGroupsByLocation status=200 elapsed=0.250s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes=2",
        ])

    def test_debug_identifies_loaded_version_and_response_body_size(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        light = self.light(credentials)
        for body in (b"", b"{}", b"<html>PRIVATE_RESPONSE</html>", b" \r\n"):
            with self.subTest(body=body):
                self.replies.append((200, body, {}))
                before = len(self.sent)
                # Logs must identify the imported handler, even if the manifest
                # on disk changes after startup or DEBUG is enabled later.
                with patch.object(credentials_module, "INTEGRATION_VERSION",
                                  "offline-loaded-build", create=True), \
                        self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
                    if body in (b"", b"{}"):
                        light.set_color(25)
                    else:
                        with self.assertRaises(ApiError):
                            light.set_color(25)
                self.assertEqual(len(self.sent), before + 1)
                self.assertEqual(len(captured.records), 1)
                diagnostic = captured.records[0].getMessage()
                self.assertIn("version=offline-loaded-build", diagnostic)
                self.assertIn(f"response_bytes={len(body)}", diagnostic)
                self.assertNotIn("PRIVATE_RESPONSE", diagnostic)

    def test_loaded_diagnostic_version_matches_integration_manifest(self):
        from havenlighting.version import INTEGRATION_VERSION

        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "custom_components" / "haven" / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(INTEGRATION_VERSION, manifest["version"])
        self.assertEqual(credentials_module.INTEGRATION_VERSION, INTEGRATION_VERSION)

    def test_debug_counts_401_refresh_and_retry_as_three_attempts(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        refresh_data = {
            "token": "replacement-token", "refreshToken": "replacement-refresh", "id": 123,
        }
        self.replies.extend([
            (401, {}, {}),
            (200, refresh_data, {}),
            (200, {}, {}),
        ])
        with self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            credentials.make_request("GET", "/LightAndZones/OrderedList/987654")

        self.assertEqual(len(self.sent), 3)
        self.assertEqual([record.getMessage() for record in captured.records], [
            "Haven HTTP attempt: method=GET endpoint=/LightAndZones/OrderedList status=401 elapsed=0.000s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes=2",
            "Haven HTTP attempt: method=POST endpoint=/Auth/Refresh status=200 elapsed=0.000s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes={len(json.dumps(refresh_data).encode())}",
            "Haven HTTP attempt: method=GET endpoint=/LightAndZones/OrderedList status=200 elapsed=0.000s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes=2",
        ])
        for secret in ("audit-token", "audit-refresh", "replacement-token", "replacement-refresh", "987654"):
            self.assertNotIn(secret, "\n".join(captured.output))

    def test_debug_counts_timeout_once_without_exception_details(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))

        def send(adapter, request, **kwargs):
            self.sent.append((adapter, request, kwargs, self.clock.monotonic()))
            self.clock.advance(0.5)
            raise requests.exceptions.Timeout("PRIVATE_EXCEPTION_DETAIL")

        with patch.object(requests.adapters.HTTPAdapter, "send", new=send), \
                self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(ApiError):
                credentials.make_request("POST", "/Commands/Off", json={"id": 987654})

        self.assertEqual(len(self.sent), 1)
        self.assertEqual([record.getMessage() for record in captured.records], [
            "Haven HTTP attempt: method=POST endpoint=/Commands/Off status=request_failed elapsed=0.500s "
            f"version={credentials_module.INTEGRATION_VERSION} response_bytes=unavailable",
        ])
        self.assertNotIn("PRIVATE_EXCEPTION_DETAIL", "\n".join(captured.output))

    def test_debug_does_not_count_local_cooldown_rejections(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.append((429, {}, {"Retry-After": "3600"}))
        with self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(ApiError):
                credentials.make_request("GET", "/LightAndZones/OrderedList/987654")
        attempts = [record for record in captured.records if record.getMessage().startswith("Haven HTTP attempt:")]
        self.assertEqual(len(attempts), 1)
        self.assertIn("status=429", attempts[0].getMessage())

        operations = (
            lambda: credentials.authenticate("audit@example.invalid", "DUMMY_PASSWORD"),
            credentials.refresh_token,
            lambda: credentials.make_request("GET", "/LightAndZones/OrderedList/987654"),
            lambda: credentials.make_request("POST", "/Commands/On", json={"id": 987654}),
        )
        with self.assertNoLogs(credentials_module.logger, level="DEBUG"):
            for operation in operations:
                with self.assertRaises(ApiError) as blocked:
                    operation()
                self.assertTrue(blocked.exception.rate_limited_locally)
        self.assertEqual(len(self.sent), 1)

    def test_debug_excludes_secrets_urls_identifiers_and_arbitrary_paths(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.extend([
            (200, {"token": "PRIVATE_ACCESS_TOKEN", "refreshToken": "PRIVATE_REFRESH_TOKEN", "id": 123}, {}),
            (200, {"private": "PRIVATE_RESPONSE_BODY"}, {"Retry-After": "PRIVATE_RESPONSE_HEADER"}),
            (200, {}, {}),
            (200, {}, {}),
        ])
        with self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            credentials.authenticate("private-email@example.invalid", "PRIVATE_PASSWORD")
            credentials.make_request(
                "GET", "/LightAndZones/OrderedList/987654?token=PRIVATE_QUERY#PRIVATE_FRAGMENT",
                headers={"X-Private": "PRIVATE_REQUEST_HEADER"},
            )
            credentials.make_request(
                "POST", "/Commands/SetColor",
                json={"id": 987654, "colorId": 11, "private": "PRIVATE_REQUEST_BODY"},
            )
            credentials.make_request("PRIVATE_METHOD", "/PRIVATE_PATH/987654?token=PRIVATE_QUERY")

        output = "\n".join(captured.output)
        for secret in (
            "PRIVATE_", "private-email@example.invalid", "987654", "Bearer",
            "stg-api.havenlighting.com", credentials._session_device_id,
        ):
            self.assertNotIn(secret, output)
        attempts = [record.getMessage() for record in captured.records if record.getMessage().startswith("Haven HTTP attempt:")]
        self.assertEqual(len(attempts), len(self.sent))
        self.assertEqual(len(attempts), 4)
        self.assertIn("method=POST endpoint=/Auth/Authenticate", attempts[0])
        self.assertIn("method=GET endpoint=/LightAndZones/OrderedList", attempts[1])
        self.assertIn("method=POST endpoint=/Commands/SetColor", attempts[2])
        self.assertIn("method=OTHER endpoint=other", attempts[3])

    def test_successful_attempt_logging_is_debug_only(self):
        credentials = self.credentials(RequestGate(min_request_interval=0))
        self.replies.append((200, {}, {}))
        with self.assertNoLogs(credentials_module.logger, level="INFO"):
            credentials.make_request("GET", "/user/GetUserInfo")
        self.assertEqual(len(self.sent), 1)

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
