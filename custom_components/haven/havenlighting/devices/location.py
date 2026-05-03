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
    MAX_POLL_INTERVAL: ClassVar[int] = 300
    SUCCESS_BACKOFF_DECAY_FACTOR: ClassVar[int] = 2
    
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
        self._consecutive_successes = 0
        
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
        if not force and (time.time() - self._last_refresh < self._poll_interval):
            return

        has_changes = False
        had_error = False
        had_rate_limit_error = False
        seen_light_ids: set[int] = set()

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
            is_rate_limited, is_transient_timeout = self._classify_refresh_error(e)
            if is_rate_limited:
                had_rate_limit_error = True
            logger.error(
                "Failed to refresh zones: %s (rate_limited=%s, transient_timeout=%s)",
                str(e),
                is_rate_limited,
                is_transient_timeout,
            )
            had_error = True

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
            is_rate_limited, is_transient_timeout = self._classify_refresh_error(e)
            if is_rate_limited:
                had_rate_limit_error = True
            logger.error(
                "Failed to refresh groups: %s (rate_limited=%s, transient_timeout=%s)",
                str(e),
                is_rate_limited,
                is_transient_timeout,
            )
            had_error = True

        stale_light_ids = set(self._lights.keys()) - seen_light_ids
        for stale_light_id in stale_light_ids:
            del self._lights[stale_light_id]
            has_changes = True

        previous_poll_interval = self._poll_interval
        if had_error:
            self._consecutive_successes = 0
            backoff_multiplier = 2 if had_rate_limit_error else self.SUCCESS_BACKOFF_DECAY_FACTOR
            self._poll_interval = min(self._poll_interval * backoff_multiplier, self.MAX_POLL_INTERVAL)
        else:
            self._consecutive_successes += 1
            if self._poll_interval > self.MIN_POLL_INTERVAL:
                self._poll_interval = max(
                    self.MIN_POLL_INTERVAL,
                    self._poll_interval // self.SUCCESS_BACKOFF_DECAY_FACTOR,
                )

        if self._poll_interval != previous_poll_interval:
            logger.info(
                "Location %s poll interval changed from %ss to %ss "
                "(had_error=%s, rate_limited=%s, consecutive_successes=%s)",
                self._location_id,
                previous_poll_interval,
                self._poll_interval,
                had_error,
                had_rate_limit_error,
                self._consecutive_successes,
            )

        self._last_refresh = time.time()

    def _classify_refresh_error(self, error: Exception) -> tuple[bool, bool]:
        """Return flags for (is_rate_limited, is_transient_timeout)."""
        if isinstance(error, ApiError):
            error_message = str(error)
            if "429" in error_message:
                return True, False
            if "timeout" in error_message.lower():
                return False, True
        return False, False

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
        previous_poll_interval = self._poll_interval
        self._poll_interval = self.MIN_POLL_INTERVAL
        self._last_refresh = 0
        self._consecutive_successes = 0
        if self._poll_interval != previous_poll_interval:
            logger.info(
                "Location %s poll interval reset from %ss to %ss due to user activity",
                self._location_id,
                previous_poll_interval,
                self._poll_interval,
            )

    def get_lights(self) -> Dict[int, Light]:
        if not self._lights:
            self.refresh_devices()
        return self._lights
