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
    RATE_LIMIT_BACKOFF_STEPS: ClassVar[tuple[int, ...]] = (60, 300, 900)
    STABLE_SUCCESS_THRESHOLD: ClassVar[int] = 3
    
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
        self._consecutive_429 = 0
        self._rate_limited_until = 0.0
        self._rate_limit_error_suppressed = False
        
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
            return
        if not force and (now - self._last_refresh < self._poll_interval):
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
            self._log_refresh_error("zones", e, is_rate_limited, is_transient_timeout)
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
            self._log_refresh_error("groups", e, is_rate_limited, is_transient_timeout)
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
            if self._consecutive_successes >= self.STABLE_SUCCESS_THRESHOLD:
                self._consecutive_429 = 0
                self._rate_limited_until = 0.0
                self._rate_limit_error_suppressed = False
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

        self._last_refresh = now

    def _classify_refresh_error(self, error: Exception) -> tuple[bool, bool]:
        """Return flags for (is_rate_limited, is_transient_timeout)."""
        if isinstance(error, ApiError):
            if self._is_rate_limit_error(error):
                self._apply_rate_limit_backoff(error)
                return True, False
            error_message = str(error)
            if "timeout" in error_message.lower():
                return False, True
        return False, False

    def _is_rate_limit_error(self, error: ApiError) -> bool:
        if getattr(error, "code", None) == 429:
            return True
        return "429" in str(error)

    def _apply_rate_limit_backoff(self, error: ApiError) -> None:
        self._consecutive_429 += 1
        step_index = min(self._consecutive_429 - 1, len(self.RATE_LIMIT_BACKOFF_STEPS) - 1)
        cooldown_seconds = self.RATE_LIMIT_BACKOFF_STEPS[step_index]
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, int):
            cooldown_seconds = min(max(cooldown_seconds, retry_after), self.RATE_LIMIT_BACKOFF_STEPS[-1])

        now = time.time()
        new_rate_limited_until = now + cooldown_seconds
        extended = new_rate_limited_until > self._rate_limited_until
        self._rate_limited_until = max(self._rate_limited_until, new_rate_limited_until)

        if not self._rate_limit_error_suppressed:
            logger.warning(
                "Location %s entering rate-limit cooldown for %ss after 429 (retry_after=%s, consecutive_429=%s)",
                self._location_id,
                cooldown_seconds,
                retry_after,
                self._consecutive_429,
            )
            self._rate_limit_error_suppressed = True
        elif extended:
            logger.info(
                "Location %s rate-limit cooldown extended to %.0f (epoch seconds)",
                self._location_id,
                self._rate_limited_until,
            )

    def _log_refresh_error(
        self,
        source: str,
        error: Exception,
        is_rate_limited: bool,
        is_transient_timeout: bool,
    ) -> None:
        if is_rate_limited and self._rate_limit_error_suppressed:
            return
        logger.error(
            "Failed to refresh %s: %s (rate_limited=%s, transient_timeout=%s)",
            source,
            str(error),
            is_rate_limited,
            is_transient_timeout,
        )

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
