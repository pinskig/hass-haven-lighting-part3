"""Discover and atomically publish a location's independently keyed targets."""

from __future__ import annotations

import threading
import time
from typing import Any, ClassVar

from ..credentials import Credentials
from ..exceptions import ApiError
from ..models import LightState, LocationData, TargetKey
from ..parsing import parse_boolean, parse_integer, parse_name, parse_target_state
from .light import Light


class Location:
    MIN_CAPABILITY_LEVEL: ClassVar[int] = 0
    MIN_POLL_INTERVAL: ClassVar[int] = 60

    def __init__(
        self,
        credentials: Credentials,
        location_id: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._location_id = parse_integer(location_id, "location ID", minimum=1)
        self._data = LocationData(
            location_id=self._location_id,
            name=data.get("name", str(self._location_id)),
            owner_name=data.get("ownerName", ""),
        ) if data else None
        self._lights: dict[TargetKey, Light] = {}
        self._last_refresh: float | None = None
        self._poll_interval = self.MIN_POLL_INTERVAL
        self._real_location_name: str | None = None
        # HTTP and commands use the operation lock. Snapshot readers only take
        # the separate state lock, which is never held during network calls.
        self._refresh_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._refresh_generation = 0
        self._last_refresh_error: Exception | None = None

    @property
    def location_id(self) -> int:
        return self._location_id

    @property
    def name(self) -> str:
        with self._state_lock:
            if self._real_location_name:
                return self._real_location_name
            if self._data:
                return self._data.owner_name or self._data.name
            return str(self._location_id)

    @property
    def lights(self) -> dict[TargetKey, Light]:
        """Return stable command targets; keys include location and target kind."""
        with self._state_lock:
            return self._lights.copy()

    @property
    def snapshot(self) -> dict[TargetKey, LightState]:
        """Copy immutable states without acquiring the network/operation lock."""
        with self._state_lock:
            return {key: light.state for key, light in self._lights.items()}

    @classmethod
    def discover(cls, credentials: Credentials) -> dict[int, Location]:
        response = credentials.make_request("GET", "/user/GetUserInfo", use_prod_api=True)
        if not isinstance(response, dict):
            raise ApiError("Invalid Haven user information response")
        loc_id = parse_integer(response.get("defaultLocationId"), "defaultLocationId", minimum=1)
        owner_parts = []
        for field in ("firstName", "lastName"):
            value = response.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            owner_parts.append(parse_name(value, field))
        loc_data = {"name": str(loc_id), "ownerName": " ".join(owner_parts)}
        return {loc_id: cls(credentials, loc_id, loc_data)}

    def refresh_and_snapshot(self, force: bool = False) -> dict[TargetKey, LightState]:
        """Executor convenience for a refresh followed by an immutable snapshot."""
        self.refresh_devices(force)
        return self.snapshot

    def execute_commands(
        self,
        key: TargetKey,
        commands: list[tuple[str, tuple[Any, ...]]],
    ) -> dict[TargetKey, LightState]:
        """Resolve a current target and execute one batch without poll interleave.

        Validate the complete command description before any side effects. A
        remote failure may still occur after an earlier command succeeded; those
        accepted changes remain in the snapshot while the original error escapes.
        """
        if not isinstance(key, TargetKey) or key.location_id != self._location_id:
            raise ApiError("Haven command target belongs to a different location")
        allowed = {"turn_on": 0, "turn_off": 0, "set_brightness": 1, "set_color": 1}
        prepared = []
        try:
            specifications = list(commands)
        except TypeError as error:
            raise ApiError("Invalid Haven command batch") from error
        for specification in specifications:
            if not isinstance(specification, (list, tuple)) or len(specification) != 2:
                raise ApiError("Invalid Haven command specification")
            name, arguments = specification
            if not isinstance(name, str) or name not in allowed:
                raise ApiError("Unsupported Haven command")
            if not isinstance(arguments, (list, tuple)) or len(arguments) != allowed[name]:
                raise ApiError(f"Invalid argument count for Haven command {name}")
            arguments = tuple(arguments)
            if name == "set_brightness":
                arguments = (parse_integer(arguments[0], "brightness", maximum=10),)
            elif name == "set_color":
                arguments = (parse_integer(arguments[0], "colorId"),)
            prepared.append((name, arguments))

        with self._refresh_lock:
            with self._state_lock:
                target = self._lights.get(key)
            if target is None:
                raise ApiError("Haven target is no longer in this location's inventory")
            for name, arguments in prepared:
                getattr(target, name)(*arguments)
            return self.snapshot

    def refresh_devices(self, force: bool = False) -> None:
        """Coalesce overlap and publish only a fully validated inventory."""
        generation = self._refresh_generation
        with self._refresh_lock:
            if generation != self._refresh_generation:
                if self._last_refresh_error is not None:
                    raise self._last_refresh_error
                return

            now = time.monotonic()
            if (
                not force
                and self._last_refresh_error is None
                and self._last_refresh is not None
                and now - self._last_refresh < max(self.MIN_POLL_INTERVAL, self._poll_interval)
            ):
                return

            try:
                zone_response = self._credentials.make_request(
                    "GET", f"/LightAndZones/OrderedList/{self._location_id}", use_prod_api=True
                )
                staged: dict[TargetKey, LightState] = {}
                location_name = self._real_location_name
                for item in self._collection(zone_response, "zones"):
                    is_zone = parse_boolean(item.get("isZone"), "isZone")
                    raw_name = item.get("locationName")
                    if raw_name is not None and not (isinstance(raw_name, str) and not raw_name.strip()):
                        candidate_name = parse_name(raw_name, "locationName")
                        if not location_name:
                            location_name = candidate_name
                    if is_zone:
                        state = parse_target_state(self._location_id, "Zone", item)
                        self._stage_unique(staged, state)

                # Any zones error stops before sending the second request.
                group_response = self._credentials.make_request(
                    "GET", f"/Group/AllGroupsByLocation/{self._location_id}", use_prod_api=True
                )
                for item in self._collection(group_response, "groups"):
                    state = parse_target_state(
                        self._location_id,
                        "Group",
                        item,
                        id_field="groupId",
                        name_field="groupName",
                        brightness_fields=("brightnessId", "lightBrightnessId"),
                    )
                    self._stage_unique(staged, state)

                # All parsing completed before the first live object changes.
                # Keys cannot collide across endpoint kinds, and old snapshots
                # remain unchanged when command targets receive new states.
                with self._state_lock:
                    published: dict[TargetKey, Light] = {}
                    for key, state in staged.items():
                        target = self._lights.get(key)
                        if target is None:
                            target = Light.from_state(
                                self._credentials,
                                state,
                                operation_lock=self._refresh_lock,
                                state_lock=self._state_lock,
                            )
                        else:
                            target._publish_state(state)
                        published[key] = target
                    self._lights = published
                    self._real_location_name = location_name
                    self._last_refresh = time.monotonic()
                    self._last_refresh_error = None
            except (KeyError, TypeError, ValueError) as error:
                api_error = ApiError(f"Invalid Haven location data: {error}")
                self._last_refresh_error = api_error
                raise api_error from error
            except Exception as error:
                self._last_refresh_error = error
                raise
            finally:
                self._refresh_generation += 1

    @staticmethod
    def _stage_unique(staged: dict[TargetKey, LightState], state: LightState) -> None:
        if state.key in staged:
            raise ApiError(
                f"Duplicate Haven {state.key.kind} ID {state.key.remote_id} "
                f"in location {state.key.location_id}"
            )
        staged[state.key] = state

    @staticmethod
    def _collection(response: Any, source: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items = response
        elif isinstance(response, dict) and isinstance(response.get("data"), list):
            items = response["data"]
        else:
            raise ApiError(f"Invalid Haven {source} response: expected a list")
        if not all(isinstance(item, dict) for item in items):
            raise ApiError(f"Invalid Haven {source} response: expected objects")
        return items

    def mark_user_activity(self) -> None:
        """Compatibility hook: commands never reset the polling floor/cooldown."""

    def get_lights(self) -> dict[TargetKey, Light]:
        """Discover once, preserving a valid empty inventory as a cached result."""
        if self._last_refresh is None:
            self.refresh_devices()
        return self.lights
