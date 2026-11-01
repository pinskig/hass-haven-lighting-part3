"""Commands for one stable target, backed by immutable state snapshots."""

from __future__ import annotations

from dataclasses import replace
import threading
from typing import Any

from ..credentials import Credentials
from ..exceptions import ApiError
from ..models import LightState, TargetKey, TargetKind
from ..parsing import haven_to_ha_brightness, parse_integer, parse_target_state


class Light:
    """Keep command identity fixed when another target shares its number."""

    def __init__(
        self,
        credentials: Credentials,
        location_id: int | None = None,
        light_id: int | None = None,
        data: dict[str, Any] | None = None,
        *,
        kind: TargetKind = "Zone",
        state: LightState | None = None,
        operation_lock: Any = None,
        state_lock: Any = None,
    ) -> None:
        # Preserve the old zone constructor. Discovery supplies parsed states.
        if state is None:
            state = parse_target_state(location_id, kind, data)
            expected_id = parse_integer(light_id, "light ID", minimum=1)
            if state.key.remote_id != expected_id:
                raise ApiError("Light ID does not match its payload")
        elif not isinstance(state, LightState):
            raise ValueError("state must be a LightState")
        elif any(value is not None for value in (location_id, light_id, data)):
            raise ValueError("Provide either state or legacy constructor fields")

        self._credentials = credentials
        self._key = state.key
        self._state = state
        self._operation_lock = operation_lock if operation_lock is not None else threading.Lock()
        self._state_lock = state_lock if state_lock is not None else threading.RLock()

    @classmethod
    def from_state(
        cls,
        credentials: Credentials,
        state: LightState,
        *,
        operation_lock: Any = None,
        state_lock: Any = None,
    ) -> Light:
        return cls(
            credentials,
            state=state,
            operation_lock=operation_lock,
            state_lock=state_lock,
        )

    @property
    def key(self) -> TargetKey:
        return self._key

    @property
    def kind(self) -> TargetKind:
        return self._key.kind

    @property
    def id(self) -> int:
        return self._key.remote_id

    @property
    def location_id(self) -> int:
        return self._key.location_id

    @property
    def state(self) -> LightState:
        with self._state_lock:
            return self._state

    @property
    def name(self) -> str:
        return self.state.name

    @property
    def is_on(self) -> bool:
        return self.state.is_on

    @property
    def brightness(self) -> int:
        return self.state.brightness

    @property
    def color_id(self) -> int | None:
        return self.state.color_id

    @property
    def _type(self) -> TargetKind:
        """Read-only compatibility alias; command kind is part of the key."""
        return self.kind

    def _publish_state(self, state: LightState) -> None:
        """Publish a parsed record without changing this object's identity."""
        if state.key != self._key:
            raise ValueError("Cannot replace a light with another target's state")
        with self._state_lock:
            self._state = state

    def update_from_data(self, data: dict[str, Any]) -> bool:
        """Compatibility helper for a normalized record of this same target."""
        candidate = parse_target_state(self.location_id, self.kind, data)
        with self._operation_lock:
            previous = self.state
            self._publish_state(candidate)
            return previous != candidate

    def _send_command(self, endpoint: str, *, changes: dict[str, Any], **values: Any) -> None:
        # Shared with the location's full poll, so an older poll cannot publish
        # over a command that completed while its network requests were pending.
        with self._operation_lock:
            payload = {"id": self.id, "type": self.kind, **values}
            self._credentials.make_request(
                "POST", endpoint, json=payload, use_prod_api=True, allow_empty_response=True
            )
            with self._state_lock:
                self._state = replace(self._state, **changes)

    def turn_on(self) -> None:
        self._send_command("/Commands/On", changes={"is_on": True})

    def turn_off(self) -> None:
        self._send_command("/Commands/Off", changes={"is_on": False})

    def set_brightness(self, level: int) -> None:
        level = parse_integer(level, "brightness", maximum=10)
        self._send_command(
            "/Commands/Brightness",
            changes={"brightness": haven_to_ha_brightness(level), "is_on": True},
            brightness=level,
        )

    def set_color(self, color_id: int) -> None:
        color_id = parse_integer(color_id, "colorId")
        self._send_command("/Commands/SetColor", changes={"color_id": color_id}, colorId=color_id)
