"""Allowlisted DEBUG descriptions; never serialize payloads or account data."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from .havenlighting.colors import (
    COLOR_BY_ID,
    HAVEN_COLOR_MAP,
    HAVEN_PATTERN_MAP,
    KELVIN_BY_ID,
    PATTERN_BY_ID,
    RGB_BY_ID,
)
from .havenlighting.models import LightState, TargetKey


# References correlate records within this process, not across installations or
# restarts. A random keyed digest does not expose emails, IDs, or entity names.
_REFERENCE_KEY = secrets.token_bytes(16)


def private_reference(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return None
    return hashlib.blake2s(
        value.encode(), key=_REFERENCE_KEY, digest_size=6
    ).hexdigest()


def target_reference(account_id: str, key: TargetKey) -> str:
    return private_reference(
        f"{account_id}:{key.location_id}:{key.kind}:{key.remote_id}"
    ) or "unknown"


def _number(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def action_description(
    values: dict[str, Any], context: Any = None, *, turn_off: bool = False
) -> dict[str, Any]:
    """Describe only typed light inputs and anonymous service-context links."""
    effect = values.get("effect")
    if turn_off:
        mode = "off"
    elif isinstance(effect, str) and effect in HAVEN_COLOR_MAP:
        mode = "color_preset"
    elif "color_temp_kelvin" in values:
        mode = "color_temp"
    elif "rgb_color" in values:
        mode = "rgb"
    elif effect == "off":
        mode = "legacy_color_reset"
    elif isinstance(effect, str) and effect in HAVEN_PATTERN_MAP:
        mode = "effect"
    elif "brightness" in values:
        mode = "brightness"
    else:
        mode = "on"
    rgb = values.get("rgb_color")
    if not (
        isinstance(rgb, (tuple, list)) and len(rgb) == 3
        and all(type(channel) is int and 0 <= channel <= 255 for channel in rgb)
    ):
        rgb = None
    return sanitize_action_description({
        "mode": mode,
        "kelvin": _number(values.get("color_temp_kelvin")),
        "rgb": tuple(rgb) if rgb is not None else None,
        "brightness": _number(values.get("brightness")),
        # The old effect input also accepts named colors. Describe that
        # compatibility path without presenting the color as a native pattern.
        "color_preset": effect
        if isinstance(effect, str) and effect in HAVEN_COLOR_MAP else None,
        "effect": effect
        if isinstance(effect, str) and effect in HAVEN_PATTERN_MAP else None,
        "context": private_reference(getattr(context, "id", None)),
        "parent": private_reference(getattr(context, "parent_id", None)),
        "has_user": bool(getattr(context, "user_id", None)),
    })


def sanitize_action_description(values: dict[str, Any] | None) -> dict[str, Any]:
    """Enforce the allowlist again at the logger boundary, including new callers."""
    values = values if isinstance(values, dict) else {}
    mode = values.get("mode")
    color_preset = values.get("color_preset")
    effect = values.get("effect")
    rgb = values.get("rgb")
    if not (
        isinstance(rgb, (tuple, list)) and len(rgb) == 3
        and all(type(channel) is int and 0 <= channel <= 255 for channel in rgb)
    ):
        rgb = None

    def reference(field: str) -> str | None:
        value = values.get(field)
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value) else None

    kelvin = _number(values.get("kelvin"))
    brightness = _number(values.get("brightness"))
    return {
        "mode": mode if isinstance(mode, str) and mode in {
            "off", "on", "color_temp", "rgb", "color_preset", "effect",
            "legacy_color_reset", "brightness",
        } else "unknown",
        "kelvin": kelvin if kelvin is not None and kelvin <= 10000 else None,
        "rgb": tuple(rgb) if rgb is not None else None,
        "brightness": brightness if brightness is not None and brightness <= 255 else None,
        "color_preset": color_preset
        if isinstance(color_preset, str) and color_preset in HAVEN_COLOR_MAP else None,
        "effect": effect
        if isinstance(effect, str) and effect in HAVEN_PATTERN_MAP else None,
        "context": reference("context"),
        "parent": reference("parent"),
        "has_user": values.get("has_user") is True,
    }


def command_description(commands: list[tuple[str, tuple]]) -> list[tuple[str, tuple]]:
    """Do not log arbitrary command names/arguments even on invalid input."""
    result = []
    for specification in commands:
        if not isinstance(specification, (list, tuple)) or len(specification) != 2:
            result.append(("invalid", ()))
            continue
        name, arguments = specification
        safe_name = name if isinstance(name, str) and name in {
            "turn_on", "turn_off", "set_color", "set_brightness",
            "reset_legacy_color", "set_white",
        } else "unknown"
        safe_arguments = (
            tuple(_number(value) for value in arguments)
            if isinstance(arguments, (tuple, list)) else ()
        )
        result.append((safe_name, safe_arguments))
    return result


def state_description(state: LightState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    color_id = state.color_id
    mode = (
        "color_temp" if color_id in KELVIN_BY_ID else
        "rgb" if color_id in COLOR_BY_ID else
        "brightness" if color_id in PATTERN_BY_ID else "unknown"
    )
    return {
        "on": state.is_on,
        "brightness": state.brightness,
        "color_id": color_id,
        "mode": mode,
        "kelvin": KELVIN_BY_ID.get(color_id),
        "rgb": RGB_BY_ID.get(color_id),
        "color_preset": COLOR_BY_ID.get(color_id),
        "effect": PATTERN_BY_ID.get(color_id),
    }
