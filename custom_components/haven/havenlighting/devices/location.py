"""Discover and refresh a shared Haven location without duplicate requests."""

from typing import Any, ClassVar, Dict, Optional
import threading
import time

from ..credentials import Credentials
from ..exceptions import ApiError
from ..models import LocationData
from .light import Light


class Location:
    MIN_CAPABILITY_LEVEL: ClassVar[int] = 0
    MIN_POLL_INTERVAL: ClassVar[int] = 60

    def __init__(
        self,
        credentials: Credentials,
        location_id: int,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._credentials = credentials
        self._location_id = location_id
        self._data = (
            LocationData(
                location_id=location_id,
                name=data.get("name", str(location_id)),
                owner_name=data.get("ownerName", ""),
            )
            if data
            else None
        )
        self._lights: Dict[int, Light] = {}
        self._last_refresh: Optional[float] = None
        self._poll_interval = self.MIN_POLL_INTERVAL
        self._real_location_name: Optional[str] = None
        self._refresh_lock = threading.Lock()
        self._refresh_generation = 0
        self._last_refresh_error: Optional[Exception] = None

    @property
    def location_id(self) -> int:
        """Return the stable location identifier."""
        return self._location_id

    @property
    def name(self) -> str:
        """Return the location name discovered from Haven."""
        if self._real_location_name:
            return self._real_location_name
        if self._data:
            return self._data.owner_name or self._data.name
        return str(self._location_id)

    @property
    def lights(self) -> Dict[int, Light]:
        """Return a snapshot of the cached mapping without fetching data."""
        return self._lights.copy()

    @classmethod
    def discover(cls, credentials: Credentials) -> Dict[int, "Location"]:
        response = credentials.make_request("GET", "/user/GetUserInfo", use_prod_api=True)
        if not isinstance(response, dict):
            raise ApiError("Invalid Haven user information response")
        locations = {}
        if "defaultLocationId" in response:
            loc_id = int(response["defaultLocationId"])
            loc_data = {
                "name": str(loc_id),
                "ownerName": (
                    f"{response.get('firstName', '')} {response.get('lastName', '')}"
                ).strip(),
            }
            locations[loc_id] = cls(credentials, loc_id, loc_data)
        return locations

    def refresh_devices(self, force: bool = False) -> None:
        """Fetch each collection once and publish only a complete valid result.

        Concurrent callers share the outcome of the refresh already in flight,
        including callers using force. The coordinator may bypass the normal
        cache interval, but every request still passes through Credentials' shared
        rate-limit gate. A failed refresh leaves the previous cache untouched.
        """
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
                and now - self._last_refresh
                < max(self.MIN_POLL_INTERVAL, self._poll_interval)
            ):
                return

            try:
                zone_response = self._credentials.make_request(
                    "GET",
                    f"/LightAndZones/OrderedList/{self._location_id}",
                    use_prod_api=True,
                )
                zone_list = self._collection(zone_response, "zones")
                staged_lights: Dict[int, Light] = {}
                location_name = self._real_location_name

                for item in zone_list:
                    if "isZone" not in item:
                        raise ApiError("Invalid Haven zones response: missing isZone")
                    if not location_name and item.get("locationName"):
                        location_name = str(item["locationName"])
                    if item.get("isZone"):
                        light = self._stage_light(item, is_group=False)
                        staged_lights[light.id] = light

                # No catch-and-continue: a zones failure must stop the refresh
                # before another endpoint is called, particularly after a 429.
                group_response = self._credentials.make_request(
                    "GET",
                    f"/Group/AllGroupsByLocation/{self._location_id}",
                    use_prod_api=True,
                )
                group_list = self._collection(group_response, "groups")
                for item in group_list:
                    group_data = {
                        "id": item["groupId"],
                        "name": item["groupName"],
                        "isOn": item["isOn"],
                        "lightBrightnessId": item.get("brightnessId", 10),
                        "colorId": item.get("colorId"),
                        "isZone": False,
                        "type": "Group",
                    }
                    light = self._stage_light(group_data, is_group=True)
                    staged_lights[light.id] = light

                # All network calls and parsing succeeded. Keep existing Light
                # objects because the Home Assistant entities hold references to
                # them; publish additions/removals only after full validation.
                for light_id, staged in tuple(staged_lights.items()):
                    if existing := self._lights.get(light_id):
                        existing._data = staged._data
                        existing._type = staged._type
                        staged_lights[light_id] = existing
                self._lights = staged_lights
                self._real_location_name = location_name
                self._last_refresh = time.monotonic()
                self._last_refresh_error = None
            except (KeyError, TypeError, ValueError) as error:
                api_error = ApiError(f"Invalid Haven location data: {error}")
                self._last_refresh_error = api_error
                raise api_error from error
            except Exception as error:
                # Preserve ApiError/code/retry_after and authentication errors
                # so the coordinator can mark entities unavailable and back off.
                self._last_refresh_error = error
                raise
            finally:
                self._refresh_generation += 1

    @staticmethod
    def _collection(response: Any, source: str) -> list[Dict[str, Any]]:
        """Reject malformed payloads instead of treating them as empty lists."""
        if isinstance(response, list):
            items = response
        elif isinstance(response, dict) and isinstance(response.get("data"), list):
            items = response["data"]
        else:
            raise ApiError(f"Invalid Haven {source} response: expected a list")
        if not all(isinstance(item, dict) for item in items):
            raise ApiError(f"Invalid Haven {source} response: expected objects")
        return items

    def _stage_light(self, data: Dict[str, Any], is_group: bool) -> Light:
        """Validate a candidate without changing any previously cached light."""
        normalized = dict(data)
        light_id = int(normalized["id"])
        normalized["id"] = light_id
        normalized["lightId"] = light_id
        normalized.setdefault("type", "Group" if is_group else "Zone")
        normalized["lightBrightnessId"] = int(
            normalized.get("lightBrightnessId", normalized.get("brightnessId", 10))
        )
        return Light(self._credentials, self._location_id, light_id, normalized)

    def mark_user_activity(self) -> None:
        """Keep the compatibility hook without resetting polling or cooldowns.

        Command callers request a debounced refresh from the shared coordinator.
        User activity must not clear a server-imposed cooldown or accelerate all
        entities' background polling.
        """

    def get_lights(self) -> Dict[int, Light]:
        """Discover lights once, including a valid empty discovery result."""
        if self._last_refresh is None:
            self.refresh_devices()
        return self.lights
