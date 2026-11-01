"""Parse Haven payloads before publishing any changes to cached state."""

from typing import Any
import re

from .exceptions import ApiError
from .models import LightState, TargetKey, TargetKind


_DECIMAL_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z", flags=re.ASCII)


def parse_integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Accept integers or canonical decimal strings, never booleans or floats."""
    if type(value) is int:
        result = value
    elif isinstance(value, str) and _DECIMAL_INTEGER.fullmatch(value):
        try:
            result = int(value)
        except ValueError as error:
            raise ApiError(f"Invalid Haven {field}: integer is too large") from error
    else:
        raise ApiError(f"Invalid Haven {field}: expected an integer")
    if result < minimum or (maximum is not None and result > maximum):
        bound = f"{minimum}–{maximum}" if maximum is not None else f"at least {minimum}"
        raise ApiError(f"Invalid Haven {field}: expected {bound}")
    return result


def parse_boolean(value: Any, field: str) -> bool:
    """Do not turn strings such as 'false' into an affirmative state."""
    if type(value) is not bool:
        raise ApiError(f"Invalid Haven {field}: expected a boolean")
    return value


def parse_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f"Invalid Haven {field}: expected a nonempty string")
    return value.strip()


def haven_to_ha_brightness(level: Any) -> int:
    """Convert a validated 0–10 Haven brightness into the HA 0–255 scale."""
    return round(parse_integer(level, "brightness", maximum=10) * 255 / 10)


def parse_target_state(
    location_id: int,
    kind: TargetKind,
    data: dict[str, Any],
    *,
    id_field: str = "id",
    name_field: str = "name",
    brightness_fields: tuple[str, ...] = ("lightBrightnessId", "brightnessId"),
) -> LightState:
    """Use the trusted endpoint kind, never a payload-supplied 'type' field."""
    if not isinstance(data, dict):
        raise ApiError("Invalid Haven target: expected an object")
    if kind not in ("Zone", "Group"):
        raise ApiError("Invalid Haven target kind")
    location = parse_integer(location_id, "location ID", minimum=1)
    remote_id = parse_integer(data.get(id_field), id_field, minimum=1)
    name = parse_name(data.get(name_field), name_field)
    is_on = parse_boolean(data.get("isOn"), "isOn")
    # Some responses omit brightness. Preserve the supported full-brightness
    # default, but validate every value supplied by the selected endpoint.
    level = next((data[field] for field in brightness_fields if field in data), 10)
    color = data.get("colorId")
    if color is not None:
        color = parse_integer(color, "colorId")
    return LightState(
        key=TargetKey(location, kind, remote_id),
        name=name,
        is_on=is_on,
        brightness=haven_to_ha_brightness(level),
        color_id=color,
    )
