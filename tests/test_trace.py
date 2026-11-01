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
            "effect": None, "context": None, "parent": None, "has_user": False,
        })
        self.assertNotIn("PRIVATE_", repr(sanitized))

        public_fields = {
            "mode": "color_temp", "kelvin": 3200, "rgb": None, "brightness": 128,
            "effect": None, "context": "0123456789ab", "parent": "abcdef012345",
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
            ("stop_effect", ()),
            {"password": "PRIVATE_PASSWORD"},
        ])
        self.assertEqual(described, [
            ("unknown", (None, None, 42)), ("set_color", (None,)),
            ("set_brightness", (None,)), ("stop_effect", ()), ("invalid", ()),
        ])
        self.assertNotIn("PRIVATE_", repr(described))


if __name__ == "__main__":
    unittest.main()
