"""Platform for Haven light integration."""
from __future__ import annotations
import logging
import math
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .havenlighting import HavenClient
from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

# --- MAPPING TABLES ---

HAVEN_KELVIN_MAP = {
    2700: 1, 3000: 2, 3500: 3, 3700: 4,
    4000: 5, 4100: 6, 4700: 7, 5000: 8,
}

HAVEN_RGB_MAP = {
    (255, 0, 0): 11,      # Red
    (255, 100, 0): 13,    # Pumpkin
    (255, 191, 0): 14,    # Amber
    (255, 128, 0): 15,    # Tangerine
    (255, 215, 0): 16,    # Marigold
    (255, 255, 0): 18,    # Yellow
    (191, 255, 0): 19,    # Lime
    (128, 255, 0): 20,    # Light Green
    (0, 255, 0): 21,      # Green
    (0, 255, 128): 22,    # Sea Foam
    (64, 224, 208): 23,   # Turquoise
    (0, 0, 255): 25,      # Deep Blue
    (127, 0, 255): 26,    # Violet
    (128, 0, 128): 27,    # Purple
    (230, 230, 250): 28,  # Lavender
    (255, 192, 203): 29,  # Pink
    (255, 105, 180): 30,  # Hot Pink
}

HAVEN_EFFECT_MAP = {
    "Fire": 12,
    "Sunset": 17,
    "Ocean": 24
}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Haven Light from a config entry."""
    client: HavenClient = hass.data[DOMAIN][config_entry.entry_id]
    
    locations = await hass.async_add_executor_job(client.discover_locations)
    if not locations:
        return
        
    # FIX 2: Register the "Location" device first to stop the "via_device" warning
    device_registry = dr.async_get(hass)
    for loc_id, location in locations.items():
        device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, str(loc_id))},
            manufacturer="Haven",
            name=location.name,
            model="Haven Controller/Location",
        )

    entities = []
    found_entity_ids = set()
    found_device_ids = set()

    for loc_key, location in locations.items():
        lights = await hass.async_add_executor_job(location.get_lights)
        if lights:
            for light in lights.values():
                entity = HavenLight(light, location)
                entities.append(entity)
                found_entity_ids.add(entity.unique_id)
                found_device_ids.add(str(light.id))

    async_add_entities(entities)
    
    await _async_cleanup_dead_items(hass, config_entry, found_entity_ids, found_device_ids)

async def _async_cleanup_dead_items(hass: HomeAssistant, config_entry: ConfigEntry, found_entity_ids: set[str], found_device_ids: set[str]):
    """Remove entities and devices that are no longer returned by the API."""
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
    for entry in entries:
        if entry.unique_id not in found_entity_ids:
            _LOGGER.info("Removing dead entity: %s", entry.entity_id)
            ent_reg.async_remove(entry.entity_id)

    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    for device in devices:
        # Skip the Location device (it won't be in the light list)
        is_location = False
        for domain, device_id in device.identifiers:
             if domain == DOMAIN and device_id not in found_device_ids:
                 # Crude check: if the ID is short (5 digits like 28513) it's likely the location, keep it.
                 # Lights are usually longer (6 digits like 801348 or 79633)
                 if len(str(device_id)) < 6: 
                     is_location = True
                     continue
                 
                 if not is_location:
                    _LOGGER.info("Removing dead device: %s", device_id)
                    dev_reg.async_remove_device(device.id)
                    break

class HavenLight(LightEntity):
    """Representation of a Haven Light."""

    _attr_has_entity_name = True
    
    # FIX 1: Removed ColorMode.BRIGHTNESS to stop the "invalid color modes" warning
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(HAVEN_EFFECT_MAP.keys())
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 5000

    def __init__(self, light, location) -> None:
        """Initialize a Haven Light."""
        self._light = light
        self._location = location
        self._attr_unique_id = f"haven_light_{light.id}"
        self._attr_name = light.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(light.id))},
            name=light.name,
            manufacturer="Haven",
            model=f"Haven {light._type}",
            # Now safe to reference because we created it in setup
            via_device=(DOMAIN, str(location._location_id)),
        )

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def is_on(self) -> bool:
        return self._light.is_on

    @property
    def brightness(self) -> int:
        return self._light.brightness

    @property
    def color_mode(self) -> ColorMode:
        # If we are in White mode, report COLOR_TEMP so the UI shows the slider
        # Otherwise report RGB
        # (For now, default to RGB to keep it simple as HA handles the switch well)
        return ColorMode.RGB

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        self._location.mark_user_activity()
        
        if ATTR_BRIGHTNESS in kwargs:
            ha_brightness = kwargs[ATTR_BRIGHTNESS]
            haven_brightness = round(ha_brightness / 25.5)
            if haven_brightness == 0: haven_brightness = 1
            await self.hass.async_add_executor_job(self._light.set_brightness, haven_brightness)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs[ATTR_EFFECT]
            if effect_name in HAVEN_EFFECT_MAP:
                await self.hass.async_add_executor_job(self._light.set_color, HAVEN_EFFECT_MAP[effect_name])
                return

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            closest_id = min(HAVEN_KELVIN_MAP.items(), key=lambda x: abs(x[0] - kelvin))[1]
            await self.hass.async_add_executor_job(self._light.set_color, closest_id)
            return

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            closest_id = self._find_closest_color_id(r, g, b)
            await self.hass.async_add_executor_job(self._light.set_color, closest_id)
            return

        if not kwargs:
            await self.hass.async_add_executor_job(self._light.turn_on)
        
        await self.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        self._location.mark_user_activity()
        await self.hass.async_add_executor_job(self._light.turn_off)
        await self.async_update()

    async def async_update(self) -> None:
        """Fetch new state data for this light."""
        await self.hass.async_add_executor_job(self._location.refresh_devices, False)

    def _find_closest_color_id(self, r, g, b):
        closest_dist = float('inf')
        closest_id = 24 
        for color_rgb, color_id in HAVEN_RGB_MAP.items():
            dist = math.sqrt((r - color_rgb[0]) ** 2 + (g - color_rgb[1]) ** 2 + (b - color_rgb[2]) ** 2)
            if dist < closest_dist:
                closest_dist = dist
                closest_id = color_id
        return closest_id
