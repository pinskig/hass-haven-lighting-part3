"""Platform for Haven light integration."""
from __future__ import annotations
import logging
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting.models import LightState, TargetKey
from .registry import async_prepare_registries, entity_unique_id

try:
    from homeassistant.components.light import EFFECT_OFF
except ImportError:  # Home Assistant 2024.1 predates this convenience constant.
    EFFECT_OFF = "off"

_LOGGER = logging.getLogger(__name__)

# Polls are centralized in the coordinator. Serialize entity service calls too.
PARALLEL_UPDATES = 1

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

KELVIN_BY_ID = {color_id: kelvin for kelvin, color_id in HAVEN_KELVIN_MAP.items()}
RGB_BY_ID = {color_id: rgb for rgb, color_id in HAVEN_RGB_MAP.items()}
EFFECT_BY_ID = {color_id: effect for effect, color_id in HAVEN_EFFECT_MAP.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add newly discovered targets while retaining missing registry entries."""
    coordinators: list[HavenCoordinator] = hass.data[DOMAIN][config_entry.entry_id]["coordinators"]
    if not coordinators:
        return

    warned: set[str] = set()
    async_prepare_registries(hass, config_entry, coordinators, warned)

    def watch_inventory(coordinator: HavenCoordinator) -> None:
        known: set[TargetKey] = set()

        @callback
        def add_new_entities() -> None:
            if not coordinator.last_update_success:
                return
            current = coordinator.data or {}
            added = current.keys() - known
            if not added:
                return
            # Reappearing legacy targets can be migrated when they are first
            # observed, even if the initial inventory was empty or incomplete.
            async_prepare_registries(hass, config_entry, coordinators, warned)
            keys = sorted(added, key=lambda key: (key.kind, key.remote_id))
            entities = [HavenLight(key, coordinator) for key in keys]
            known.update(keys)
            async_add_entities(entities)

        config_entry.async_on_unload(coordinator.async_add_listener(add_new_entities))
        add_new_entities()

    for coordinator in coordinators:
        watch_inventory(coordinator)

class HavenLight(CoordinatorEntity[HavenCoordinator], LightEntity):
    """Representation of a Haven Light."""

    _attr_has_entity_name = True
    _attr_name = None

    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}

    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [*HAVEN_EFFECT_MAP, EFFECT_OFF]
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 5000

    def __init__(self, key: TargetKey, coordinator: HavenCoordinator) -> None:
        """Initialize a Haven Light."""
        super().__init__(coordinator)
        self._key = key
        self._last_state = coordinator.data[key]
        self._attr_unique_id = entity_unique_id(coordinator.account_id, key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._last_state.name,
            manufacturer="Haven",
            model=f"Haven {key.kind}",
        )

    @property
    def available(self) -> bool:
        return super().available and self._key in (self.coordinator.data or {})

    @property
    def _state(self) -> LightState:
        return (self.coordinator.data or {}).get(self._key, self._last_state)

    @callback
    def _handle_coordinator_update(self) -> None:
        if updated := (self.coordinator.data or {}).get(self._key):
            if updated.name != self._last_state.name:
                self._attr_device_info = {**self._attr_device_info, "name": updated.name}
                if self.registry_entry is not None and self.registry_entry.device_id:
                    dr.async_get(self.hass).async_update_device(
                        self.registry_entry.device_id, name=updated.name
                    )
            self._last_state = updated
        super()._handle_coordinator_update()

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def is_on(self) -> bool:
        return self._state.is_on

    @property
    def brightness(self) -> int:
        return self._state.brightness

    @property
    def color_mode(self) -> ColorMode:
        if self._state.color_id in KELVIN_BY_ID:
            return ColorMode.COLOR_TEMP
        if self._state.color_id in RGB_BY_ID:
            return ColorMode.RGB
        if self._state.color_id in EFFECT_BY_ID:
            return ColorMode.BRIGHTNESS
        return ColorMode.UNKNOWN

    @property
    def color_temp_kelvin(self) -> int | None:
        return KELVIN_BY_ID.get(self._state.color_id)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return RGB_BY_ID.get(self._state.color_id)

    @property
    def effect(self) -> str | None:
        color_id = self._state.color_id
        if color_id in EFFECT_BY_ID:
            return EFFECT_BY_ID[color_id]
        if color_id in KELVIN_BY_ID or color_id in RGB_BY_ID:
            return EFFECT_OFF
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        commands: list[tuple[str, tuple]] = []
        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            if brightness == 0:
                await self.async_turn_off()
                return
            commands.append(("set_brightness", (max(1, round(brightness / 25.5)),)))

        color_id = None
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None and effect != EFFECT_OFF:
            if effect not in HAVEN_EFFECT_MAP:
                raise HomeAssistantError(f"Unknown Haven effect: {effect}")
            color_id = HAVEN_EFFECT_MAP[effect]
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            color_id = min(HAVEN_KELVIN_MAP, key=lambda value: abs(value - kelvin))
            color_id = HAVEN_KELVIN_MAP[color_id]
        elif ATTR_RGB_COLOR in kwargs:
            color_id = self._find_closest_color_id(*kwargs[ATTR_RGB_COLOR])
        elif effect == EFFECT_OFF and self._state.color_id in EFFECT_BY_ID:
            # A static white preset stops an effect when no replacement color
            # was supplied. Haven has no separate effect-stop endpoint here.
            color_id = HAVEN_KELVIN_MAP[2700]

        if color_id is not None:
            commands.append(("set_color", (color_id,)))
        # Cached power can lag the app or a schedule. Brightness implies on;
        # other turn_on requests must explicitly power the target on.
        if ATTR_BRIGHTNESS not in kwargs:
            commands.append(("turn_on", ()))
        await self.coordinator.async_send_commands(self._key, commands)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_send_commands(self._key, [("turn_off", ())])

    @staticmethod
    def _find_closest_color_id(r: int, g: int, b: int) -> int:
        rgb = min(
            HAVEN_RGB_MAP,
            key=lambda value: (r - value[0]) ** 2 + (g - value[1]) ** 2 + (b - value[2]) ** 2,
        )
        return HAVEN_RGB_MAP[rgb]
