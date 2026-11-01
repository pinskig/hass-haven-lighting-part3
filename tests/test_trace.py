"""Privacy guards for diagnostic values supplied by current or future callers."""

import importlib
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


class _UnsafeValue:
    def __repr__(self):
        raise AssertionError("An arbitrary caller object reached diagnostic formatting")


class TracePrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load these pure helpers without importing the HA integration entrypoint.
        root = Path(__file__).resolve().parents[1] / "custom_components" / "haven"
        namespace = "haven_trace_test"
        package = ModuleType(namespace)
        package.__path__ = [str(root)]
        library = ModuleType(f"{namespace}.havenlighting")
        library.__path__ = [str(root / "havenlighting")]
        with patch.dict(sys.modules, {namespace: package, f"{namespace}.havenlighting": library}):
            cls.trace = importlib.import_module(f"{namespace}.trace")

    def test_action_boundary_discards_unknown_keys_and_private_wrong_types(self):
        sanitized = self.trace.sanitize_action_description({
            "mode": "PRIVATE_MODE",
            "kelvin": "PRIVATE_KELVIN",
            "rgb": (255, 0, "PRIVATE_RGB"),
            "brightness": True,
            "color_preset": "PRIVATE_COLOR_PRESET",
            "effect": "PRIVATE_EFFECT",
            "context": "PRIVATE_EMAIL@example.invalid",
            "parent": "0123456789ab\n",
            "has_user": "PRIVATE_USER_ID",
            "password": "PRIVATE_PASSWORD",
            "headers": {"Authorization": "PRIVATE_TOKEN"},
            "payload": _UnsafeValue(),
        })
        self.assertEqual(sanitized, {
            "mode": "unknown", "kelvin": None, "rgb": None, "brightness": None,
            "color_preset": None, "effect": None,
            "context": None, "parent": None, "has_user": False,
        })
        self.assertNotIn("PRIVATE_", repr(sanitized))

        public_fields = {
            "mode": "color_temp", "kelvin": 3200, "rgb": None, "brightness": 128,
            "color_preset": None, "effect": None,
            "context": "0123456789ab", "parent": "abcdef012345",
            "has_user": True,
        }
        self.assertEqual(
            self.trace.sanitize_action_description({**public_fields, "payload": _UnsafeValue()}),
            public_fields,
        )

    def test_command_descriptions_do_not_format_arbitrary_labels_or_arguments(self):
        described = self.trace.command_description([
            ("PRIVATE_COMMAND", ("PRIVATE_ARGUMENT", _UnsafeValue(), 42)),
            ("set_color", ("PRIVATE_COLOR",)),
            ("set_brightness", (True,)),
            ("reset_legacy_color", ()),
            ("stop_effect", ()),
            {"password": "PRIVATE_PASSWORD"},
        ])
        self.assertEqual(described, [
            ("unknown", (None, None, 42)), ("set_color", (None,)),
            ("set_brightness", (None,)), ("reset_legacy_color", ()),
            ("unknown", ()), ("invalid", ()),
        ])
        self.assertNotIn("PRIVATE_", repr(described))

    def test_legacy_effect_inputs_are_described_as_named_colors(self):
        for name in ("Red", "Fire", "Sunset", "Ocean"):
            with self.subTest(name=name):
                described = self.trace.action_description({"effect": name, "brightness": 128})
                self.assertEqual(described["mode"], "color_preset")
                self.assertEqual(described["color_preset"], name)
                self.assertIsNone(described["effect"])
                self.assertEqual(described["brightness"], 128)

    def test_legacy_reset_label_preserves_explicit_color_precedence(self):
        cases = [
            ({"effect": "off"}, "legacy_color_reset"),
            ({"effect": "off", "color_temp_kelvin": 3500}, "color_temp"),
            ({"effect": "off", "rgb_color": (0, 0, 255)}, "rgb"),
        ]
        for values, expected_mode in cases:
            with self.subTest(values=values):
                described = self.trace.action_description(values)
                self.assertEqual(described["mode"], expected_mode)
                self.assertIsNone(described["effect"])
                self.assertIsNone(described["color_preset"])

    def test_named_colors_and_patterns_have_separate_diagnostic_allowlists(self):
        for name in ("Warm Colors", "Cool Colors", "All Colors"):
            with self.subTest(name=name):
                described = self.trace.action_description({"effect": name})
                self.assertEqual(described["mode"], "effect")
                self.assertEqual(described["effect"], name)
                self.assertIsNone(described["color_preset"])
        mislabeled = self.trace.sanitize_action_description({
            "mode": "color_preset", "color_preset": "Warm Colors", "effect": "Fire",
        })
        self.assertIsNone(mislabeled["color_preset"])
        self.assertIsNone(mislabeled["effect"])

    def test_reported_palette_colors_are_not_native_effects(self):
        for color_id, name in ((12, "Fire"), (17, "Sunset"), (24, "Ocean")):
            with self.subTest(color_id=color_id):
                state = self.trace.LightState(
                    self.trace.TargetKey(123456, "Zone", 101), "PRIVATE_NAME", True, 128, color_id,
                )
                described = self.trace.state_description(state)
                self.assertEqual(described["mode"], "rgb")
                self.assertEqual(described["color_preset"], name)
                self.assertIsNone(described["effect"])
                self.assertEqual(described["brightness"], 128)
                if color_id == 24:
                    self.assertIsNone(described["rgb"])
                self.assertNotIn("PRIVATE_NAME", repr(described))

    def test_reported_native_patterns_and_unknown_values_are_distinct(self):
        cases = [
            (33, "brightness", "Warm Colors"),
            (34, "brightness", "Cool Colors"),
            (35, "brightness", "All Colors"),
            (6, "color_temp", None),
            (999, "unknown", None),
            (None, "unknown", None),
        ]
        for color_id, mode, effect in cases:
            with self.subTest(color_id=color_id):
                state = self.trace.LightState(
                    self.trace.TargetKey(123456, "Zone", 101), "PRIVATE_NAME", True, 204, color_id,
                )
                described = self.trace.state_description(state)
                self.assertEqual(described["mode"], mode)
                self.assertEqual(described["effect"], effect)
                self.assertIsNone(described["color_preset"])
                self.assertIsNone(described["rgb"])
                self.assertEqual(described["brightness"], 204)


if __name__ == "__main__":
    unittest.main()
