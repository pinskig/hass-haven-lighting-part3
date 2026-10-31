"""Offline regressions for Haven's shared request budget and location refreshes.

Every HTTP request is intercepted at requests.request. No credentials or access
to a Haven account are needed. The library is imported independently of Home
Assistant so these tests also run on an ordinary Python installation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "haven"))

from havenlighting import credentials as credentials_module
from havenlighting.credentials import Credentials
from havenlighting.devices import location as location_module
from havenlighting.devices.light import Light
from havenlighting.devices.location import Location
from havenlighting.exceptions import ApiError


class Clock:
    """A deterministic clock; advancing it never sleeps or calls the network."""

    def __init__(self) -> None:
        self.elapsed = 1_000.0
        self.epoch = 1_800_000_000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.elapsed

    def time(self) -> float:
        with self._lock:
            return self.epoch + self.elapsed

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.elapsed += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class ObservedLock:
    """Observe acquisition attempts so overlap tests need no timing sleeps."""

    def __init__(self, participants: int) -> None:
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._attempts = 0
        self._participants = participants
        self.all_attempting = threading.Event()

    def __enter__(self):
        with self._count_lock:
            self._attempts += 1
            if self._attempts == self._participants:
                self.all_attempting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *args):
        self._lock.release()


def response(status: int = 200, data=None, headers=None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.invalid/haven-test"
    result.headers.update(headers or {})
    result._content = json.dumps({} if data is None else data).encode("utf-8")
    result.encoding = "utf-8"
    return result


def zone(zone_id: int = 101, *, is_on: bool = True) -> dict:
    return {"id": zone_id, "isZone": True, "name": "Test zone", "isOn": is_on,
            "lightBrightnessId": 5, "colorId": 11}


def group(group_id: int = 202, *, is_on: bool = True) -> dict:
    return {"groupId": group_id, "groupName": "Test group", "isOn": is_on,
            "brightnessId": 5, "colorId": 11}


class OfflineHavenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        for module in (credentials_module, location_module):
            timer = patch.object(module, "time", self.clock)
            timer.start()
            self.addCleanup(timer.stop)

        network = patch.object(requests, "request")
        self.request = network.start()
        self.addCleanup(network.stop)
        self.request.side_effect = AssertionError("Unexpected HTTP request in offline test")

        self.credentials = Credentials(min_request_interval=0.0)
        self.credentials._update_credentials(
            {"token": "old-token", "refreshToken": "test-refresh-token", "id": 1}
        )
        self.location = Location(self.credentials, 12345, {"name": "Test location"})

    @staticmethod
    def location_response(method: str, url: str, **kwargs) -> requests.Response:
        if "/LightAndZones/OrderedList/" in url:
            return response(data=[zone()])
        if "/Group/AllGroupsByLocation/" in url:
            return response(data=[group()])
        raise AssertionError(f"Unexpected request: {method} {url}")


class LocationRefreshTests(OfflineHavenTest):
    def test_eight_sequential_entity_updates_share_one_refresh(self) -> None:
        self.request.side_effect = self.location_response
        for _ in range(8):
            self.location.refresh_devices()
        self.assertEqual(self.request.call_count, 2)

        self.clock.advance(60)
        self.location.refresh_devices()
        self.assertEqual(self.request.call_count, 4)

    def test_eight_concurrent_entity_updates_share_one_refresh(self) -> None:
        start = threading.Barrier(9)
        request_entered = threading.Event()
        release_request = threading.Event()

        def server(method, url, **kwargs):
            if "/LightAndZones/OrderedList/" in url:
                request_entered.set()
                if not release_request.wait(3):
                    raise AssertionError("Test did not release the mocked request")
            return self.location_response(method, url, **kwargs)

        def update():
            start.wait(timeout=3)
            return self.location.refresh_devices()

        self.request.side_effect = server
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(update) for _ in range(8)]
            try:
                start.wait(timeout=3)
                self.assertTrue(request_entered.wait(3))
            finally:
                release_request.set()
            for future in futures:
                future.result(timeout=3)

        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(set(self.location.get_lights()), {101, 202})

    def test_overlapping_forced_refreshes_share_the_inflight_result(self) -> None:
        observed = ObservedLock(8)
        self.location._refresh_lock = observed
        release_request = threading.Event()

        def server(method, url, **kwargs):
            if "/LightAndZones/OrderedList/" in url:
                if not release_request.wait(3):
                    raise AssertionError("Test did not release the mocked request")
            return self.location_response(method, url, **kwargs)

        self.request.side_effect = server
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.location.refresh_devices, True) for _ in range(8)]
            try:
                # The generation has been sampled by every caller before the
                # first network response is released. This forces true overlap.
                self.assertTrue(observed.all_attempting.wait(3))
            finally:
                release_request.set()
            for future in futures:
                future.result(timeout=3)

        self.assertEqual(self.request.call_count, 2)

    def test_overlapping_forced_refreshes_share_the_rate_limit_failure(self) -> None:
        observed = ObservedLock(8)
        self.location._refresh_lock = observed
        release_request = threading.Event()

        def server(*args, **kwargs):
            if not release_request.wait(3):
                raise AssertionError("Test did not release the mocked request")
            return response(429, headers={"Retry-After": "3600"})

        self.request.side_effect = server
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.location.refresh_devices, True) for _ in range(8)]
            try:
                self.assertTrue(observed.all_attempting.wait(3))
            finally:
                release_request.set()
            for future in futures:
                with self.assertRaises(ApiError) as raised:
                    future.result(timeout=3)
                self.assertEqual(raised.exception.code, 429)

        self.assertEqual(self.request.call_count, 1)

    def test_zone_429_stops_before_requesting_groups(self) -> None:
        self.request.return_value = response(429, headers={"Retry-After": "3600"})
        self.request.side_effect = None
        with self.assertRaises(ApiError) as raised:
            self.location.refresh_devices()
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(self.request.call_count, 1)
        self.assertIn("/LightAndZones/OrderedList/", self.request.call_args.args[1])

    def test_force_refresh_cannot_bypass_account_cooldown(self) -> None:
        self.request.side_effect = [response(429, headers={"Retry-After": "3600"})]
        with self.assertRaises(ApiError):
            self.location.refresh_devices()
        self.clock.advance(1)
        with self.assertRaises(ApiError) as raised:
            self.location.refresh_devices(force=True)
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(self.request.call_count, 1)

    def test_group_failure_preserves_cached_state_and_object_identity(self) -> None:
        self.request.side_effect = self.location_response
        self.location.refresh_devices()
        original = self.location.get_lights().copy()
        self.clock.advance(60)
        self.request.side_effect = [response(data=[zone(is_on=False), zone(303)]), response(500)]

        with self.assertRaises(ApiError):
            self.location.refresh_devices()

        preserved = self.location.get_lights()
        self.assertEqual(set(preserved), {101, 202})
        for light_id in preserved:
            self.assertIs(preserved[light_id], original[light_id])
            self.assertTrue(preserved[light_id].is_on)

    def test_success_after_failure_updates_existing_objects(self) -> None:
        self.request.side_effect = self.location_response
        self.location.refresh_devices()
        original = self.location.get_lights().copy()
        self.clock.advance(60)
        self.request.side_effect = [response(data=[zone(is_on=False)]), response(500)]
        with self.assertRaises(ApiError):
            self.location.refresh_devices()

        self.clock.advance(60)
        self.request.side_effect = [response(data=[zone(is_on=False)]), response(data=[group(is_on=False)])]
        self.location.refresh_devices()
        updated = self.location.get_lights()
        for light_id in updated:
            self.assertIs(updated[light_id], original[light_id])
            self.assertFalse(updated[light_id].is_on)

    def test_malformed_zone_data_never_deletes_cached_lights(self) -> None:
        self.request.side_effect = self.location_response
        self.location.refresh_devices()
        original = self.location.get_lights().copy()
        invalid_payloads = [{}, {"data": {}}, ["invalid record"], [{"id": 101, "isOn": False}]]
        for data in invalid_payloads:
            with self.subTest(data=data):
                previous_calls = self.request.call_count
                self.request.side_effect = [response(data=data)]
                with self.assertRaises(ApiError):
                    self.location.refresh_devices(force=True)
                self.assertEqual(self.request.call_count, previous_calls + 1)
                preserved = self.location.get_lights()
                self.assertEqual(set(preserved), {101, 202})
                for light_id in preserved:
                    self.assertIs(preserved[light_id], original[light_id])
                    self.assertTrue(preserved[light_id].is_on)

    def test_empty_success_is_cached_without_an_extra_discovery(self) -> None:
        self.request.side_effect = [response(data=[]), response(data=[])]
        self.location.refresh_devices()
        for _ in range(8):
            self.assertEqual(self.location.get_lights(), {})
        self.assertEqual(self.request.call_count, 2)

    def test_success_does_not_return_to_five_second_polling(self) -> None:
        self.request.side_effect = self.location_response
        for cycle in range(8):
            self.location.refresh_devices()
            expected = (cycle + 1) * 2
            self.assertEqual(self.request.call_count, expected)
            self.clock.advance(5)
            self.location.refresh_devices()
            self.assertEqual(self.request.call_count, expected)
            self.clock.advance(55)

    def test_user_activity_preserves_the_shared_polling_floor(self) -> None:
        self.request.side_effect = self.location_response
        self.location.refresh_devices()
        self.clock.advance(5)
        self.location.mark_user_activity()
        self.location.refresh_devices()
        self.assertEqual(self.request.call_count, 2)


class SharedRequestLimitTests(OfflineHavenTest):
    def trigger_limit(self, retry_after: str = "3600") -> ApiError:
        self.request.side_effect = [response(429, headers={"Retry-After": retry_after})]
        with self.assertRaises(ApiError) as raised:
            self.credentials.make_request("GET", "/test/poll")
        self.assertEqual(raised.exception.code, 429)
        return raised.exception

    def test_default_budget_spaces_outbound_requests_at_least_one_second(self) -> None:
        credentials = Credentials()
        credentials._update_credentials({"token": "test-token", "id": 1})
        sent_at = []

        def server(*args, **kwargs):
            sent_at.append(self.clock.monotonic())
            return response(data={"ok": True})

        self.request.side_effect = server
        for method, path in [("GET", "/test/zones"), ("GET", "/test/groups"),
                             ("POST", "/Commands/On"), ("POST", "/Commands/Off")]:
            credentials.make_request(method, path)
        self.assertEqual(len(sent_at), 4)
        for first, second in zip(sent_at, sent_at[1:]):
            self.assertGreaterEqual(second - first, 1.0)

    def test_poll_429_blocks_commands_authentication_and_token_refresh(self) -> None:
        self.trigger_limit()
        operations = [
            lambda: self.credentials.make_request("POST", "/Commands/On", json={"id": 101}),
            lambda: self.credentials.authenticate("test@example.invalid", "test-password"),
            self.credentials.refresh_token,
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ApiError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, 429)
                self.assertTrue(raised.exception.rate_limited_locally)
        self.assertEqual(self.request.call_count, 1)

    def test_command_429_blocks_the_next_poll(self) -> None:
        self.request.side_effect = [response(429, headers={"Retry-After": "3600"})]
        with self.assertRaises(ApiError):
            self.credentials.make_request("POST", "/Commands/On", json={"id": 101})
        with self.assertRaises(ApiError) as raised:
            self.location.refresh_devices()
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(self.request.call_count, 1)

    def test_light_command_reports_rejection_and_keeps_its_state(self) -> None:
        self.trigger_limit()
        light = Light(self.credentials, 12345, 101, zone(is_on=False))
        with self.assertRaises(ApiError) as raised:
            light.turn_on()
        self.assertEqual(raised.exception.code, 429)
        self.assertFalse(light.is_on)
        self.assertEqual(self.request.call_count, 1)

    def test_retry_after_hour_is_honored_without_a_fifteen_minute_cap(self) -> None:
        error = self.trigger_limit("3600")
        self.assertGreaterEqual(error.retry_after, 3600)
        self.clock.advance(901)
        with self.assertRaises(ApiError):
            self.credentials.make_request("GET", "/test/poll")
        self.assertEqual(self.request.call_count, 1)

        self.clock.advance(2699)
        self.request.side_effect = [response(data={"ok": True})]
        self.assertEqual(self.credentials.make_request("GET", "/test/poll"), {"ok": True})
        self.assertEqual(self.request.call_count, 2)

    def test_retry_after_http_date_is_honored(self) -> None:
        deadline = datetime.fromtimestamp(self.clock.time() + 3600, timezone.utc)
        error = self.trigger_limit(format_datetime(deadline, usegmt=True))
        self.assertGreaterEqual(error.retry_after, 3600)
        self.clock.advance(3599)
        with self.assertRaises(ApiError):
            self.credentials.make_request("GET", "/test/poll")
        self.assertEqual(self.request.call_count, 1)
        self.clock.advance(1)
        self.request.side_effect = [response(data={"ok": True})]
        self.assertEqual(self.credentials.make_request("GET", "/test/poll"), {"ok": True})

    def test_local_rejections_do_not_restart_or_extend_cooldown(self) -> None:
        self.trigger_limit("60")
        for elapsed in range(1, 60):
            self.clock.advance(1)
            with self.assertRaises(ApiError) as raised:
                self.credentials.make_request("POST", "/Commands/On", json={"id": 101})
            self.assertTrue(raised.exception.rate_limited_locally)
            self.assertLessEqual(raised.exception.retry_after, 60 - elapsed)
        self.assertEqual(self.request.call_count, 1)
        self.clock.advance(1)
        self.request.side_effect = [response(data={"ok": True})]
        self.assertEqual(self.credentials.make_request("GET", "/test/poll"), {"ok": True})
        self.assertEqual(self.request.call_count, 2)

    def test_token_refresh_429_preserves_rate_limit_error_and_wait(self) -> None:
        self.request.side_effect = [response(401), response(429, headers={"Retry-After": "3600"})]
        with self.assertRaises(ApiError) as raised:
            self.credentials.make_request("GET", "/test/poll")
        self.assertEqual(raised.exception.code, 429)
        self.assertGreaterEqual(raised.exception.retry_after, 3600)
        self.assertEqual(self.request.call_count, 2)
        self.assertTrue(self.request.call_args.args[1].endswith("/Auth/Refresh"))
        with self.assertRaises(ApiError):
            self.credentials.make_request("GET", "/test/poll")
        self.assertEqual(self.request.call_count, 2)

    def test_transient_authentication_and_refresh_errors_propagate(self) -> None:
        operations = [
            lambda: self.credentials.authenticate("test@example.invalid", "test-password"),
            self.credentials.refresh_token,
        ]
        for operation in operations:
            for failure in (requests.exceptions.Timeout("simulated timeout"), response(503)):
                with self.subTest(operation=operation, failure=failure):
                    self.request.side_effect = [failure]
                    with self.assertRaises(ApiError):
                        operation()
                    self.assertTrue(self.credentials.is_authenticated)
                    self.assertEqual(self.credentials._token, "old-token")

    def test_malformed_refresh_does_not_replace_working_credentials(self) -> None:
        invalid_responses = [
            {}, [], {"token": None, "id": 1}, {"token": "replacement"},
            {"token": "replacement", "id": 1, "refreshToken": None},
        ]
        for data in invalid_responses:
            with self.subTest(data=data):
                self.request.side_effect = [response(data=data)]
                with self.assertRaises(ApiError):
                    self.credentials.refresh_token()
                self.assertEqual(self.credentials._token, "old-token")
                self.assertEqual(self.credentials._refresh_token, "test-refresh-token")
                self.assertEqual(self.credentials._user_id, 1)

    def test_refresh_can_keep_an_omitted_refresh_token(self) -> None:
        self.request.side_effect = [response(data={"token": "new-token", "id": 1})]
        self.assertTrue(self.credentials.refresh_token())
        self.assertEqual(self.credentials._token, "new-token")
        self.assertEqual(self.credentials._refresh_token, "test-refresh-token")

    def test_concurrent_expired_tokens_trigger_only_one_renewal(self) -> None:
        start = threading.Barrier(9)
        refresh_entered = threading.Event()
        release_refresh = threading.Event()
        refreshed = []

        def server(method, url, **kwargs):
            if url.endswith("/Auth/Refresh"):
                refreshed.append(url)
                refresh_entered.set()
                if not release_refresh.wait(3):
                    raise AssertionError("Test did not release token refresh")
                return response(data={"token": "new-token", "refreshToken": "new-refresh-token", "id": 1})
            if kwargs["headers"].get("Authorization") == "Bearer old-token":
                return response(401)
            self.assertEqual(kwargs["headers"].get("Authorization"), "Bearer new-token")
            return response(data={"ok": True})

        def request():
            start.wait(timeout=3)
            return self.credentials.make_request("GET", "/test/poll")

        self.request.side_effect = server
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(request) for _ in range(8)]
            try:
                start.wait(timeout=3)
                self.assertTrue(refresh_entered.wait(3))
            finally:
                release_refresh.set()
            for future in futures:
                self.assertEqual(future.result(timeout=3), {"ok": True})

        self.assertEqual(len(refreshed), 1)


if __name__ == "__main__":
    unittest.main()
