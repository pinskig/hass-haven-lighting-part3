"""Offline action correlation across context, executor and HTTP boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "haven"))

from havenlighting import credentials as credentials_module
from havenlighting.credentials import Credentials
from havenlighting.exceptions import ApiError
from havenlighting.request_context import get_request_trace, new_request_trace, request_trace
from havenlighting.request_gate import RequestGate


class RequestContextTests(unittest.TestCase):
    def test_generated_trace_is_bounded_and_contains_no_identifiers(self):
        self.assertRegex(new_request_trace(), r"\A[0-9a-f]{12}\Z")

    def test_nested_context_restores_after_exception_and_exit(self):
        self.assertEqual(get_request_trace(), "-")
        with request_trace("0123456789ab"):
            self.assertEqual(get_request_trace(), "0123456789ab")
            with self.assertRaises(RuntimeError):
                with request_trace("abcdef012345"):
                    self.assertEqual(get_request_trace(), "abcdef012345")
                    raise RuntimeError("offline action failure")
            self.assertEqual(get_request_trace(), "0123456789ab")
        self.assertEqual(get_request_trace(), "-")

    def test_invalid_context_does_not_replace_active_trace(self):
        with request_trace("0123456789ab"):
            for invalid in (None, 123, "", "-", "ABCDEF012345", "123", "a" * 13,
                            "PRIVATE_EMAIL@example.invalid", "0123456789ab\n"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError) as raised:
                        with request_trace(invalid):
                            self.fail("Invalid trace entered its context")
                    self.assertEqual(get_request_trace(), "0123456789ab")
                    self.assertNotIn("PRIVATE_EMAIL", str(raised.exception))
        self.assertEqual(get_request_trace(), "-")

    def test_copy_context_propagates_to_executor_without_leaking(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(get_request_trace).result(timeout=3), "-")
            with request_trace("0123456789ab"):
                first = copy_context()
                self.assertEqual(
                    pool.submit(first.run, get_request_trace).result(timeout=3),
                    "0123456789ab",
                )
                with request_trace("abcdef012345"):
                    second = copy_context()
                    self.assertEqual(
                        pool.submit(second.run, get_request_trace).result(timeout=3),
                        "abcdef012345",
                    )
                self.assertEqual(pool.submit(get_request_trace).result(timeout=3), "-")
                self.assertEqual(get_request_trace(), "0123456789ab")
            self.assertEqual(pool.submit(get_request_trace).result(timeout=3), "-")
        self.assertEqual(get_request_trace(), "-")


class RequestTraceTransportTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials(request_gate=RequestGate(min_request_interval=0))
        self.credentials._session.trust_env = False
        self.credentials._update_credentials(
            {"token": "offline-token", "refreshToken": "offline-refresh", "id": 1}
        )
        self.addCleanup(self.credentials.close)
        self.sent = []
        self.replies = []

        def send(adapter, request, **kwargs):
            self.sent.append(request)
            if not self.replies:
                raise AssertionError("Unexpected HTTP attempt in offline trace test")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status, data, headers = reply
            response = requests.Response()
            response.status_code = status
            response.request = request
            response.url = request.url
            response.headers.update(headers)
            response._content = json.dumps(data).encode()
            response._content_consumed = True
            response.encoding = "utf-8"
            return response

        network = patch.object(requests.adapters.HTTPAdapter, "send", new=send)
        network.start()
        self.addCleanup(network.stop)

    @staticmethod
    def attempts(captured):
        return [record.getMessage() for record in captured.records
                if record.getMessage().startswith("Haven HTTP attempt:")]

    def test_401_refresh_and_retry_keep_trace_then_next_request_has_none(self):
        self.replies.extend([
            (401, {}, {}),
            (200, {"token": "replacement-token", "refreshToken": "replacement-refresh", "id": 1}, {}),
            (200, {}, {}),
            (200, {}, {}),
        ])
        with request_trace("0123456789ab"), \
                self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            self.credentials.make_request("GET", "/user/GetUserInfo")
        attempts = self.attempts(captured)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.sent), 3)
        for diagnostic in attempts:
            self.assertIn("trace=0123456789ab", diagnostic)
            self.assertNotIn("offline-token", diagnostic)
            self.assertNotIn("replacement-token", diagnostic)
        self.assertIn("endpoint=/Auth/Refresh", attempts[1])
        self.assertEqual(get_request_trace(), "-")

        with self.assertLogs(credentials_module.logger, level="DEBUG") as unscoped:
            self.credentials.make_request("GET", "/user/GetUserInfo")
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(len(self.attempts(unscoped)), 1)
        self.assertIn("trace=-", self.attempts(unscoped)[0])

    def test_http_failure_is_correlated_and_context_resets(self):
        self.replies.append(requests.exceptions.Timeout("PRIVATE_EXCEPTION_DETAIL"))
        with self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(ApiError):
                with request_trace("0123456789ab"):
                    self.credentials.make_request("POST", "/Commands/Off", json={"id": 987654})
        attempts = self.attempts(captured)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("status=request_failed", attempts[0])
        self.assertIn("trace=0123456789ab", attempts[0])
        self.assertNotIn("PRIVATE_EXCEPTION_DETAIL", attempts[0])
        self.assertNotIn("987654", attempts[0])
        self.assertEqual(get_request_trace(), "-")

    def test_local_cooldown_does_not_emit_a_correlated_http_attempt(self):
        self.replies.append((429, {}, {"Retry-After": "3600"}))
        with request_trace("0123456789ab"), \
                self.assertLogs(credentials_module.logger, level="DEBUG") as captured:
            with self.assertRaises(ApiError):
                self.credentials.make_request("POST", "/Commands/On", json={"id": 987654})
        attempts = self.attempts(captured)
        self.assertEqual(len(attempts), 1)
        self.assertIn("trace=0123456789ab", attempts[0])

        with request_trace("abcdef012345"), \
                self.assertNoLogs(credentials_module.logger, level="DEBUG"):
            with self.assertRaises(ApiError) as raised:
                self.credentials.make_request("POST", "/Commands/On", json={"id": 987654})
        self.assertTrue(raised.exception.rate_limited_locally)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(get_request_trace(), "-")


if __name__ == "__main__":
    unittest.main()
