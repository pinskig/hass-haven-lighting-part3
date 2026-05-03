from typing import Dict, Any, Optional, ClassVar
import logging
import time
from ..models import LocationData
from .light import Light
from ..credentials import Credentials
from ..exceptions import ApiError

logger = logging.getLogger(__name__)

class Location:
    MIN_CAPABILITY_LEVEL: ClassVar[int] = 0
    MIN_POLL_INTERVAL: ClassVar[int] = 5
    MAX_POLL_INTERVAL: ClassVar[int] = 80
    RATE_LIMIT_BACKOFF_SECONDS: ClassVar[tuple[int, ...]] = (60, 300, 900)
    STABLE_REFRESHES_TO_RESET: ClassVar[int] = 3
    
    def __init__(self, credentials: Credentials, location_id: int, data: Optional[Dict[str, Any]] = None) -> None:
        self._credentials = credentials
        self._location_id = location_id
        self._data = LocationData(
            location_id=location_id,
            name=data.get("name", str(location_id)),
            owner_name=data.get("ownerName", "")
        ) if data else None
        self._lights: Dict[int, Light] = {}
        self._last_refresh = 0
        self._poll_interval = self.MIN_POLL_INTERVAL
        self._real_location_name = None # Store the real name (e.g., "Crescenti Oasis")
        self._rate_limited_until = 0.0
        self._consecutive_429 = 0
        self._stable_successful_refreshes = 0
        self._cooldown_warning_emitted = False
        self._suppressed_error_signature: Optional[str] = None
        
    @property
    def name(self) -> str:
        # Return the real location name if we found it, otherwise fall back to Owner Name
        return self._real_location_name or self._data.owner_name if self._data else str(self._location_id)
        
    @classmethod
    def discover(cls, credentials: Credentials) -> Dict[int, 'Location']:
        response = credentials.make_request("GET", "/user/GetUserInfo", use_prod_api=True)
        locations = {}
        if "defaultLocationId" in response:
            loc_id = int(response["defaultLocationId"])
            loc_data = {
                "name": str(loc_id),
                "ownerName": f"{response.get('firstName', '')} {response.get('lastName', '')}".strip()
            }
            locations[loc_id] = cls(credentials, loc_id, loc_data)
        return locations

    def refresh_devices(self, force: bool = False) -> None:
        now = time.time()
        if not force and now < self._rate_limited_until:
            if not self._cooldown_warning_emitted:
                remaining = int(self._rate_limited_until - now)
                logger.warning(
                    "Location %s is rate-limited for %ss; skipping refresh.",
                    self._location_id,
                    max(remaining, 0),
                )
                self._cooldown_warning_emitted = True
            return

        if not force and (now - self._last_refresh < self._poll_interval):
            return

        has_changes = False
        had_error = False
        seen_light_ids: set[int] = set()
        saw_429_error = False
        successful_calls = 0

        # 1. Fetch Individual Zones
        try:
            response = self._credentials.make_request(
                "GET", 
                f"/LightAndZones/OrderedList/{self._location_id}", 
                use_prod_api=True
            )
            zone_list = response if isinstance(response, list) else response.get("data", [])
            for item in zone_list:
                # CAPTURE THE REAL LOCATION NAME
                if not self._real_location_name and "locationName" in item:
                    self._real_location_name = item["locationName"]
                    
                if item.get("isZone"):
                    seen_light_ids.add(int(item["id"]))
                    has_changes = self._add_or_update_light(item, is_group=False) or has_changes
        except Exception as e:
            if self._handle_api_error("zones", e):
                saw_429_error = True
            had_error = True
        else:
            successful_calls += 1

        # 2. Fetch Groups
        try:
            response = self._credentials.make_request(
                "GET", 
                f"/Group/AllGroupsByLocation/{self._location_id}", 
                use_prod_api=True
            )
            group_list = response if isinstance(response, list) else response.get("data", [])
            for item in group_list:
                group_data = {
                    "id": item["groupId"],
                    "name": item["groupName"],
                    "isOn": item["isOn"],
                    "lightBrightnessId": item.get("brightnessId", 10),
                    "colorId": item.get("colorId"),
                    "isZone": False,
                    "type": "Group"
                }
                seen_light_ids.add(int(group_data["id"]))
                has_changes = self._add_or_update_light(group_data, is_group=True) or has_changes
        except Exception as e:
            if self._handle_api_error("groups", e):
                saw_429_error = True
            had_error = True
        else:
            successful_calls += 1

        stale_light_ids = set(self._lights.keys()) - seen_light_ids
        for stale_light_id in stale_light_ids:
            del self._lights[stale_light_id]
            has_changes = True

        if had_error:
            self._poll_interval = min(self._poll_interval * 2, self.MAX_POLL_INTERVAL)
        else:
            # Keep HA-only operation responsive with a steady cadence.
            # We only back off when the API is erroring.
            self._poll_interval = self.MIN_POLL_INTERVAL

        if saw_429_error:
            self._stable_successful_refreshes = 0
        elif not had_error and successful_calls > 0:
            self._stable_successful_refreshes += 1
            if self._stable_successful_refreshes >= self.STABLE_REFRESHES_TO_RESET:
                self._consecutive_429 = 0
                self._rate_limited_until = 0.0
                self._suppressed_error_signature = None
                self._cooldown_warning_emitted = False
        else:
            self._stable_successful_refreshes = 0

        self._last_refresh = now

    def _handle_api_error(self, scope: str, error: Exception) -> bool:
        if not self._is_429_error(error):
            logger.error("Failed to refresh %s: %s", scope, str(error))
            self._stable_successful_refreshes = 0
            return False

        self._consecutive_429 += 1
        self._stable_successful_refreshes = 0
        cooldown_seconds = self._extract_retry_after_seconds(error) or self._cooldown_seconds_for_429()
        new_rate_limited_until = time.time() + cooldown_seconds
        self._rate_limited_until = max(self._rate_limited_until, new_rate_limited_until)

        error_signature = f"{scope}:{str(error)}:{cooldown_seconds}"
        if self._suppressed_error_signature != error_signature:
            logger.warning(
                "Rate limited while refreshing %s for location %s; cooling down for %ss (consecutive_429=%s).",
                scope,
                self._location_id,
                cooldown_seconds,
                self._consecutive_429,
            )
            self._suppressed_error_signature = error_signature
        self._cooldown_warning_emitted = False
        return True

    def _cooldown_seconds_for_429(self) -> int:
        idx = min(self._consecutive_429 - 1, len(self.RATE_LIMIT_BACKOFF_SECONDS) - 1)
        return self.RATE_LIMIT_BACKOFF_SECONDS[idx]

    @staticmethod
    def _is_429_error(error: Exception) -> bool:
        if isinstance(error, ApiError) and getattr(error, "code", None) == 429:
            return True
        return "429" in str(error)

    @staticmethod
    def _extract_retry_after_seconds(error: Exception) -> Optional[int]:
        retry_after_value = getattr(error, "retry_after", None)
        if retry_after_value is None:
            return None
        try:
            retry_after_seconds = int(retry_after_value)
        except (TypeError, ValueError):
            return None
        return max(retry_after_seconds, 0)

    def _add_or_update_light(self, data: Dict[str, Any], is_group: bool) -> bool:
        light_id = int(data["id"])
        if "type" not in data:
            data["type"] = "Group" if is_group else "Zone"
            
        if light_id in self._lights:
            return self._lights[light_id].update_from_data(data)

        data["lightId"] = light_id
        self._lights[light_id] = Light(
            self._credentials,
            self._location_id,
            light_id,
            data
        )
        return True

    def mark_user_activity(self) -> None:
        """Reset polling cadence to quickly capture follow-up state changes."""
        self._poll_interval = self.MIN_POLL_INTERVAL
        self._last_refresh = 0

    def get_lights(self) -> Dict[int, Light]:
        if not self._lights:
            self.refresh_devices()
        return self._lights
