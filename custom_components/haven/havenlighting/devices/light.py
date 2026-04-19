from typing import Dict, Any
import logging
from ..models import LightData
from ..credentials import Credentials

logger = logging.getLogger(__name__)

class Light:
    """Represents a Haven light device."""

    def __init__(self, credentials: Credentials, location_id: int, light_id: int, data: Dict[str, Any]) -> None:
        self._credentials = credentials
        self.location_id = location_id
        self._type = "Zone" if data.get("isZone") else "Device"
        if data.get("type"):
            self._type = data.get("type")
            
        self.update_from_data(data)
        logger.debug("Initialized Light: %s (ID: %d, Type: %s)", self.name, self.id, self._type)

    @property
    def id(self) -> int:
        return self._data.light_id

    @property
    def name(self) -> str:
        return self._data.name

    @property
    def is_on(self) -> bool:
        return self._data.status == 1

    @property
    def brightness(self) -> int:
        return int(self._data.brightness * 25.5)

    def update_from_data(self, data: Dict[str, Any]) -> bool:
        is_on = data.get("isOn", False)
        # Handle potential key mismatch between Zones (lightBrightnessId) and Groups (brightnessId)
        brightness = data.get("lightBrightnessId", data.get("brightnessId", 10))

        new_id = int(data.get("id", self._data.light_id if hasattr(self, '_data') else data.get("id")))
        new_name = data.get("name", "Unknown")
        new_status = 1 if is_on else 0
        new_color = data.get("colorId")

        has_changed = (
            not hasattr(self, "_data")
            or self._data.light_id != new_id
            or self._data.name != new_name
            or self._data.status != new_status
            or self._data.brightness != brightness
            or self._data.color != new_color
        )
        
        self._data = LightData(
            light_id=new_id,
            name=new_name,
            status=new_status,
            brightness=brightness,
            color=new_color,
            pattern_speed=None
        )
        return has_changed

    def turn_on(self) -> None:
        try:
            self._send_simple_command("/Commands/On")
            self._data.status = 1
        except Exception as e:
            logger.error("Failed to turn on %s", str(e))

    def turn_off(self) -> None:
        try:
            self._send_simple_command("/Commands/Off")
            self._data.status = 0
        except Exception as e:
            logger.error("Failed to turn off %s", str(e))

    def set_brightness(self, level: int) -> None:
        level = max(0, min(10, int(level)))
        try:
            payload = {"id": self.id, "type": self._type, "brightness": level}
            self._credentials.make_request("POST", "/Commands/Brightness", json=payload, use_prod_api=True)
            self._data.brightness = level
            self._data.status = 1 
        except Exception as e:
            logger.error("Failed to set brightness %s", str(e))

    def set_color(self, color_id: int) -> None:
        try:
            payload = {"id": self.id, "type": self._type, "colorId": int(color_id)}
            self._credentials.make_request("POST", "/Commands/SetColor", json=payload, use_prod_api=True)
            self._data.color = color_id
        except Exception as e:
            logger.error("Failed to set color %s", str(e))

    def _send_simple_command(self, endpoint: str) -> None:
        payload = {"id": self.id, "type": self._type}
        self._credentials.make_request("POST", endpoint, json=payload, use_prod_api=True)
