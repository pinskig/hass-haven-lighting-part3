"""Command acknowledgment regressions through the real Requests boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "haven"))

from havenlighting.credentials import Credentials
from havenlighting.devices.light import Light
from havenlighting.devices.location import Location
from havenlighting.exceptions import ApiError
from havenlighting.models import LightState, TargetKey
from havenlighting.request_gate import RequestGate


class CommandResultTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials(request_gate=RequestGate(min_request_interval=0))
        self.credentials._session.trust_env = False
        self.credentials._update_credentials(
            {"token": "offline-token", "refreshToken": "offline-refresh", "id": 1}
        )
        self.addCleanup(self.credentials.close)
        self.replies = []
        self.sent = []

        def send(adapter, request, **kwargs):
            self.sent.append(request)
            if not self.replies:
                raise AssertionError("Unexpected HTTP attempt in offline command test")
            status, data = self.replies.pop(0)
            response = requests.Response()
            response.status_code = status
            response.request = request
            response.url = request.url
            response._content = data if isinstance(data, bytes) else json.dumps(data).encode()
            response._content_consumed = True
            response.encoding = "utf-8"
            return response

        network = patch.object(requests.adapters.HTTPAdapter, "send", new=send)
        network.start()
        self.addCleanup(network.stop)

    def light(self, command="turn_on", kind="Zone"):
        return Light.from_state(self.credentials, LightState(
            TargetKey(123, kind, 456), "Offline target", command == "turn_off", 128, 11,
        ))

    @staticmethod
    def commands():
        return (
            ("turn_on", (), {"is_on": True}),
            ("turn_off", (), {"is_on": False}),
            ("set_brightness", (8,), {"brightness": 204, "is_on": True}),
            ("set_color", (25,), {"color_id": 25}),
        )

    def test_empty_acknowledgments_remain_accepted(self):
        for status in (200, 202, 204):
            for name, arguments, expected in self.commands():
                with self.subTest(status=status, command=name):
                    light = self.light(name)
                    self.replies.append((status, b""))
                    before = len(self.sent)
                    getattr(light, name)(*arguments)
                    self.assertEqual(len(self.sent), before + 1)
                    for field, value in expected.items():
                        self.assertEqual(getattr(light.state, field), value)

    def test_explicit_success_remains_accepted(self):
        for reply in ({"success": True}, [{"success": True}, {"success": True}]):
            for name, arguments, expected in self.commands():
                with self.subTest(reply=reply, command=name):
                    light = self.light(name)
                    self.replies.append((200, reply))
                    before = len(self.sent)
                    getattr(light, name)(*arguments)
                    self.assertEqual(len(self.sent), before + 1)
                    for field, value in expected.items():
                        self.assertEqual(getattr(light.state, field), value)

    def test_explicit_failure_does_not_publish_or_replay(self):
        failure = {"success": False, "id": 987654, "detail": "PRIVATE_RESPONSE"}
        for kind in ("Zone", "Group"):
            for reply in (failure, [failure]):
                for name, arguments, _expected in self.commands():
                    with self.subTest(kind=kind, reply=reply, command=name):
                        light = self.light(name, kind)
                        previous = light.state
                        self.replies.append((200, reply))
                        before = len(self.sent)
                        with patch.object(requests.Response, "close", autospec=True,
                                          side_effect=requests.Response.close) as close:
                            with self.assertRaises(ApiError) as raised:
                                getattr(light, name)(*arguments)
                        self.assertIs(light.state, previous)
                        self.assertEqual(len(self.sent), before + 1)
                        close.assert_called_once()
                        for private in ("PRIVATE_RESPONSE", "987654", "offline-token"):
                            self.assertNotIn(private, str(raised.exception))

    def test_mixed_results_reject_any_failure(self):
        replies = (
            [{"success": True}, {"success": False}],
            [{"success": False}, {"success": True}],
            [None, "unrecognized", {"success": False}],
        )
        for reply in replies:
            with self.subTest(reply=reply):
                light = self.light()
                previous = light.state
                self.replies.append((200, reply))
                before = len(self.sent)
                with self.assertRaises(ApiError):
                    light.set_color(25)
                self.assertIs(light.state, previous)
                self.assertEqual(len(self.sent), before + 1)

    def test_unknown_acknowledgment_shapes_keep_existing_behavior(self):
        # No full Zone/Group schema is established. Only a literal boolean false
        # in a recognized result rejects a command; do not coerce other types.
        for reply in ({}, [], None, "accepted", {"accepted": True},
                      {"success": 0}, {"success": "false"}, [None, {"id": 456}]):
            with self.subTest(reply=reply):
                light = self.light()
                self.replies.append((200, reply))
                before = len(self.sent)
                light.turn_on()
                self.assertTrue(light.is_on)
                self.assertEqual(len(self.sent), before + 1)

    def test_batch_keeps_successful_brightness_then_stops_on_color_failure(self):
        location = Location(self.credentials, 123, {"name": "Offline location"})
        key = TargetKey(123, "Zone", 456)
        self.replies.extend([
            (200, [{"id": 456, "name": "Offline target", "isZone": True,
                    "isOn": False, "lightBrightnessId": 5, "colorId": 11}]),
            (200, []),
        ])
        location.refresh_devices()
        before = len(self.sent)
        self.replies.extend([
            (200, [{"success": True}]),
            (200, [{"success": False}]),
            (200, [{"success": True}]),  # Must remain unused: no trailing On.
        ])
        with self.assertRaises(ApiError):
            location.execute_commands(key, [
                ("set_brightness", (8,)), ("set_color", (25,)), ("turn_on", ()),
            ])
        state = location.snapshot[key]
        self.assertEqual(state.brightness, 204)
        self.assertTrue(state.is_on)
        self.assertEqual(state.color_id, 11)
        self.assertEqual([urlsplit(request.url).path for request in self.sent[before:]], [
            "/api/Commands/Brightness", "/api/Commands/SetColor",
        ])
        self.assertEqual(len(self.replies), 1)


if __name__ == "__main__":
    unittest.main()
