"""Validated immutable identities and state published by the Haven client."""

from dataclasses import dataclass
from typing import Literal, Optional


TargetKind = Literal["Zone", "Group"]


@dataclass(frozen=True, slots=True)
class TargetKey:
    """Identify a target without conflating location, zone, or group IDs."""

    location_id: int
    kind: TargetKind
    remote_id: int

    def __post_init__(self) -> None:
        for name in ("location_id", "remote_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.kind not in ("Zone", "Group"):
            raise ValueError("kind must be Zone or Group")


@dataclass(frozen=True, slots=True)
class LightState:
    """One complete target state; brightness uses Home Assistant's 0–255 scale."""

    key: TargetKey
    name: str
    is_on: bool
    brightness: int
    color_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, TargetKey):
            raise ValueError("key must be a TargetKey")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a nonempty string")
        if type(self.is_on) is not bool:
            raise ValueError("is_on must be a boolean")
        if type(self.brightness) is not int or not 0 <= self.brightness <= 255:
            raise ValueError("brightness must be an integer from 0 to 255")
        if self.color_id is not None and (
            type(self.color_id) is not int or self.color_id < 0
        ):
            raise ValueError("color_id must be a nonnegative integer or None")


@dataclass(frozen=True, slots=True)
class LocationData:
    """Descriptive account data for a Haven location."""

    location_id: int
    name: str
    owner_name: Optional[str] = None
