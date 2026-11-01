"""Offline identity, validation, immutable-state, and publication regressions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "haven"))

from havenlighting.devices.light import Light
from havenlighting.devices.location import Location
from havenlighting.exceptions import ApiError
from havenlighting.models import LightState, TargetKey


def zone(remote_id=101, **changes):
    return {"id": remote_id, "name": "Test zone", "isZone": True, "isOn": True,
            "lightBrightnessId": 5, "colorId": 11, **changes}


def group(remote_id=202, **changes):
    return {"groupId": remote_id, "groupName": "Test group", "isOn": True,
            "brightnessId": 5, "colorId": 11, **changes}


class FakeApi:
    """Endpoint fixture with no network implementation or real credentials."""

    def __init__(self):
        self.zones = [zone()]
        self.groups = [group()]
        self.user = {"defaultLocationId": 123456}
        self.calls = []
        self.command_error = None
        self.on_command = lambda _path, _kwargs: None
        self.on_zone = lambda: None
        self.on_group = lambda: None

    def make_request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if method == "POST":
            self.on_command(path, kwargs)
            if self.command_error:
                raise self.command_error
            return {}
        if "OrderedList" in path:
            self.on_zone()
            result = self.zones
        elif "AllGroupsByLocation" in path:
            self.on_group()
            result = self.groups
        elif "GetUserInfo" in path:
            result = self.user
        else:
            raise AssertionError(f"Unexpected offline request {method} {path}")
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class ObservedLock:
    """Coordinate a poll and queued command without timing-based sleeps."""

    def __init__(self, notify_at, *, reentrant=False):
        self._lock = threading.RLock() if reentrant else threading.Lock()
        self._counter_lock = threading.Lock()
        self._attempts = 0
        self.notify_at = notify_at
        self.entered = threading.Event()

    def __enter__(self):
        with self._counter_lock:
            self._attempts += 1
            if self._attempts == self.notify_at:
                self.entered.set()
        self._lock.acquire()

    def __exit__(self, *args):
        self._lock.release()


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.location = Location(self.api, 123456)
        self.zone_key = TargetKey(123456, "Zone", 101)
        self.group_key = TargetKey(123456, "Group", 202)

    def test_zone_and_group_with_the_same_number_remain_independent(self):
        self.api.groups = [group(101)]
        self.location.refresh_devices(True)
        zone_target = self.location.lights[self.zone_key]
        group_key = TargetKey(123456, "Group", 101)
        group_target = self.location.lights[group_key]
        self.assertEqual(set(self.location.snapshot), {self.zone_key, group_key})
        self.assertIsNot(zone_target, group_target)
        zone_target.turn_off()
        group_target.turn_on()
        sent = [kwargs["json"] for method, _, kwargs in self.api.calls if method == "POST"]
        self.assertEqual(sent, [{"id": 101, "type": "Zone"}, {"id": 101, "type": "Group"}])
        self.assertFalse(zone_target.is_on)
        self.assertTrue(group_target.is_on)

    def test_payload_type_cannot_change_endpoint_identity(self):
        self.api.zones = [zone(type="Group")]
        self.api.groups = [group(type="Zone")]
        self.location.refresh_devices(True)
        self.assertEqual(self.location.lights[self.zone_key].kind, "Zone")
        self.assertEqual(self.location.lights[self.group_key].kind, "Group")
        target = self.location.lights[self.zone_key]
        with self.assertRaises(ValueError):
            target._publish_state(replace(target.state, key=TargetKey(123456, "Group", 101)))
        self.assertEqual(target.kind, "Zone")

    def test_old_snapshots_remain_unchanged_after_refresh_and_commands(self):
        old = self.location.refresh_and_snapshot(True)
        original_target = self.location.lights[self.zone_key]
        self.api.zones = [zone(isOn=False, lightBrightnessId=10, colorId=4)]
        new = self.location.refresh_and_snapshot(True)
        self.assertIs(self.location.lights[self.zone_key], original_target)
        self.assertTrue(old[self.zone_key].is_on)
        self.assertEqual(old[self.zone_key].brightness, 128)
        self.assertFalse(new[self.zone_key].is_on)
        self.assertEqual(new[self.zone_key].brightness, 255)
        original_target.turn_on()
        self.assertFalse(new[self.zone_key].is_on)
        self.assertTrue(self.location.snapshot[self.zone_key].is_on)

    def test_key_and_state_are_frozen_and_validate_direct_construction(self):
        state = LightState(self.zone_key, "Zone", False, 128, None)
        with self.assertRaises(FrozenInstanceError):
            state.is_on = True
        with self.assertRaises(FrozenInstanceError):
            self.zone_key.kind = "Group"
        for bad_key in [(True, "Zone", 1), (1, "Zone", 1.0), (1, "Device", 1)]:
            with self.subTest(bad_key=bad_key), self.assertRaises(ValueError):
                TargetKey(*bad_key)
        with self.assertRaises(ValueError):
            replace(state, brightness=256)
        self.assertNotEqual(self.zone_key, TargetKey(999999, "Zone", 101))

    def test_canonical_decimal_strings_are_normalized(self):
        self.api.zones = [zone("101", lightBrightnessId="5", colorId="11")]
        self.api.groups = [group("202", brightnessId="0", colorId=None)]
        state = self.location.refresh_and_snapshot(True)
        self.assertEqual(state[self.zone_key].brightness, 128)
        self.assertEqual(state[self.zone_key].color_id, 11)
        self.assertEqual(state[self.group_key].brightness, 0)
        self.assertIsNone(state[self.group_key].color_id)

    def test_invalid_zone_values_preserve_the_entire_previous_snapshot(self):
        old = self.location.refresh_and_snapshot(True)
        objects = self.location.lights
        invalid = {
            "id": [True, 0, -1, 1.5, float("nan"), float("inf"), "01", "+1", "1.0"],
            "isZone": ["false", "true", 0, None],
            "isOn": ["false", "true", 0, None],
            "lightBrightnessId": [True, -1, 11, 500, 1.5, float("nan"), "1.0", None],
            "colorId": [True, -1, 1.5, float("inf"), "false"],
            "name": [None, 123, "", "   "],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.api.zones = [zone(**{field: value})]
                    previous_calls = len(self.api.calls)
                    with self.assertRaises(ApiError):
                        self.location.refresh_devices(True)
                    self.assertEqual(len(self.api.calls), previous_calls + 1)
                    self.assertEqual(self.location.snapshot, old)
                    for key, target in objects.items():
                        self.assertIs(self.location.lights[key], target)

    def test_invalid_group_rolls_back_valid_zone_changes(self):
        old = self.location.refresh_and_snapshot(True)
        target = self.location.lights[self.zone_key]
        self.api.zones = [zone(isOn=False)]
        self.api.groups = [group(isOn="false")]
        with self.assertRaises(ApiError):
            self.location.refresh_devices(True)
        self.assertEqual(self.location.snapshot, old)
        self.assertIs(self.location.lights[self.zone_key], target)

    def test_duplicate_keys_within_either_collection_are_rejected(self):
        old = self.location.refresh_and_snapshot(True)
        for zones, groups in [([zone(101), zone("101")], [group()]),
                              ([zone()], [group(202), group("202")])]:
            with self.subTest(zones=zones, groups=groups):
                self.api.zones, self.api.groups = zones, groups
                with self.assertRaisesRegex(ApiError, "Duplicate Haven"):
                    self.location.refresh_devices(True)
                self.assertEqual(self.location.snapshot, old)

    def test_successful_commands_replace_state_only_after_acceptance(self):
        self.location.refresh_devices(True)
        target = self.location.lights[self.zone_key]
        actions = [(target.turn_off, ()), (target.turn_on, ()),
                   (target.set_brightness, (8,)), (target.set_color, (4,))]
        for action, args in actions:
            with self.subTest(action=action.__name__):
                before = target.state
                self.api.command_error = ApiError("Rejected", code=429, retry_after=60)
                with self.assertRaises(ApiError):
                    action(*args)
                self.assertIs(target.state, before)
                self.api.command_error = None
                action(*args)
                self.assertIsNot(target.state, before)
        self.assertEqual(target.brightness, 204)
        self.assertEqual(target.color_id, 4)

    def test_invalid_command_values_do_not_send_requests(self):
        self.location.refresh_devices(True)
        target = self.location.lights[self.zone_key]
        before = len(self.api.calls)
        for method, value in [(target.set_brightness, -1), (target.set_brightness, 11),
                              (target.set_brightness, True), (target.set_brightness, 1.5),
                              (target.set_color, False), (target.set_color, -1)]:
            with self.subTest(method=method, value=value), self.assertRaises(ApiError):
                method(value)
        self.assertEqual(len(self.api.calls), before)

    def test_batch_rejects_invalid_commands_before_sending_anything(self):
        before = self.location.refresh_and_snapshot(True)
        sent = len(self.api.calls)
        invalid = [
            [("turn_on", ()), ("_publish_state", (before[self.zone_key],))],
            [("turn_on", ()), ("set_brightness", (11,))],
            [("turn_on", ()), ("set_color", (False,))],
            [("turn_on", ()), ("turn_off", (1,))],
            [("turn_on", ()), ("set_color", 4)],
            [("turn_on", ()), ("turn_off",)],
            None,
        ]
        for commands in invalid:
            with self.subTest(commands=commands), self.assertRaises(ApiError):
                self.location.execute_commands(self.zone_key, commands)
            self.assertEqual(len(self.api.calls), sent)
            self.assertEqual(self.location.snapshot, before)
        with self.assertRaises(ApiError):
            self.location.execute_commands(TargetKey(999999, "Zone", 101), [("turn_on", ())])
        self.assertEqual(len(self.api.calls), sent)

    def test_batch_preserves_completed_state_after_a_later_command_failure(self):
        before = self.location.refresh_and_snapshot(True)
        failure = ApiError("Rate limited", code=429, retry_after=120)
        attempts = []

        def reject_second(path, kwargs):
            attempts.append((path, kwargs))
            if len(attempts) == 2:
                raise failure

        self.api.on_command = reject_second
        with self.assertRaises(ApiError) as raised:
            self.location.execute_commands(
                self.zone_key,
                [("set_brightness", (8,)), ("set_color", (4,)), ("turn_off", ())],
            )
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(attempts), 2)
        state = self.location.snapshot[self.zone_key]
        self.assertTrue(state.is_on)
        self.assertEqual(state.brightness, 204)
        self.assertEqual(state.color_id, before[self.zone_key].color_id)
        self.assertEqual(before[self.zone_key].brightness, 128)

    def test_batch_keeps_poll_from_interleaving_between_commands(self):
        observed = ObservedLock(notify_at=4, reentrant=True)
        self.location._refresh_lock = observed
        self.location.refresh_devices(True)
        in_command, release = threading.Event(), threading.Event()

        def wait_for_first_command(_path, _kwargs):
            if not in_command.is_set():
                in_command.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release command fixture")

        self.api.on_command = wait_for_first_command
        with ThreadPoolExecutor(max_workers=2) as executor:
            commanding = executor.submit(
                self.location.execute_commands,
                self.zone_key,
                [("set_brightness", (8,)), ("set_color", (4,))],
            )
            try:
                self.assertTrue(in_command.wait(3))
                refreshing = executor.submit(self.location.refresh_devices, True)
                self.assertTrue(observed.entered.wait(3))
                self.assertEqual([method for method, _, _ in self.api.calls], ["GET", "GET", "POST"])
            finally:
                release.set()
            commanded = commanding.result(timeout=3)
            refreshing.result(timeout=3)
        self.assertEqual(commanded[self.zone_key].brightness, 204)
        self.assertEqual(commanded[self.zone_key].color_id, 4)
        self.assertEqual(
            [method for method, _, _ in self.api.calls],
            ["GET", "GET", "POST", "POST", "GET", "GET"],
        )

    def test_batch_resolves_current_target_after_a_queued_removal_poll(self):
        observed = ObservedLock(notify_at=3, reentrant=True)
        self.location._refresh_lock = observed
        self.location.refresh_devices(True)
        self.api.zones = []
        in_group, release = threading.Event(), threading.Event()

        def wait_for_group():
            in_group.set()
            if not release.wait(3):
                raise AssertionError("Test did not release group fixture")

        self.api.on_group = wait_for_group
        with ThreadPoolExecutor(max_workers=2) as executor:
            refreshing = executor.submit(self.location.refresh_devices, True)
            try:
                self.assertTrue(in_group.wait(3))
                commanding = executor.submit(
                    self.location.execute_commands, self.zone_key, [("turn_on", ())]
                )
                self.assertTrue(observed.entered.wait(3))
            finally:
                release.set()
            refreshing.result(timeout=3)
            with self.assertRaisesRegex(ApiError, "no longer"):
                commanding.result(timeout=3)
        self.assertNotIn(self.zone_key, self.location.snapshot)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_snapshot_does_not_wait_for_a_network_response(self):
        old = self.location.refresh_and_snapshot(True)
        entered, release = threading.Event(), threading.Event()

        def wait_for_network():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release network fixture")

        self.api.on_zone = wait_for_network
        with ThreadPoolExecutor(max_workers=2) as executor:
            refreshing = executor.submit(self.location.refresh_devices, True)
            try:
                self.assertTrue(entered.wait(3))
                snapshot = executor.submit(lambda: self.location.snapshot).result(timeout=1)
                self.assertEqual(snapshot, old)
            finally:
                release.set()
            refreshing.result(timeout=3)

    def test_command_waits_for_poll_publication_and_cannot_be_overwritten(self):
        observed = ObservedLock(notify_at=3)
        self.location._refresh_lock = observed
        self.api.zones = [zone(isOn=False)]
        self.location.refresh_devices(True)  # First operation, creates shared locks.
        target = self.location.lights[self.zone_key]
        in_group, release = threading.Event(), threading.Event()

        def wait_for_group():
            in_group.set()
            if not release.wait(3):
                raise AssertionError("Test did not release group fixture")

        self.api.on_group = wait_for_group
        with ThreadPoolExecutor(max_workers=2) as executor:
            refreshing = executor.submit(self.location.refresh_devices, True)
            try:
                self.assertTrue(in_group.wait(3))
                commanding = executor.submit(target.turn_on)
                self.assertTrue(observed.entered.wait(3))
                self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
            finally:
                release.set()
            refreshing.result(timeout=3)
            commanding.result(timeout=3)
        self.assertTrue(self.location.snapshot[self.zone_key].is_on)

    def test_valid_empty_inventory_is_cached(self):
        self.api.zones = []
        self.api.groups = []
        self.assertEqual(self.location.refresh_and_snapshot(True), {})
        for _ in range(8):
            self.assertEqual(self.location.get_lights(), {})
        self.assertEqual(len(self.api.calls), 2)

    def test_invalid_discovery_identifiers_raise_api_errors(self):
        for value in [None, True, 0, -1, 1.5, "1.0"]:
            with self.subTest(value=value), self.assertRaises(ApiError):
                self.api.user = {"defaultLocationId": value}
                Location.discover(self.api)
        self.api.user = {"defaultLocationId": "123456"}
        self.assertEqual(set(Location.discover(self.api)), {123456})


if __name__ == "__main__":
    unittest.main()
