"""Platform for Haven light integration."""
from __future__ import annotations
from collections.abc import Coroutine
import logging
import time
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

from .havenlighting.colors import (
    COLOR_BY_ID, HAVEN_COLOR_MAP, HAVEN_KELVIN_MAP, HAVEN_PATTERN_MAP,
    HAVEN_RGB_MAP, KELVIN_BY_ID, PATTERN_BY_ID, RGB_BY_ID,
)
from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting.models import LightState, TargetKey
from .havenlighting.request_context import new_request_trace, request_trace
from .registry import async_prepare_registries, entity_unique_id
from .trace import action_description, target_reference

try:
    from homeassistant.components.light import EFFECT_OFF
except ImportError:  # Home Assistant 2024.1 predates this convenience constant.
    EFFECT_OFF = "off"

_LOGGER = logging.getLogger(__name__)

# Keep HA's service serialization: it resolves toggle/brightness steps before
# entering our entity methods. Allowing those calls through early loses their
# relative intent. The coordinator and account locks also serialize device I/O.
PARALLEL_UPDATES = 1

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

    # HA filters effect inputs before dispatch without this feature bit. Keep
    # legacy named-color actions working, but hide the incorrect effect menu.
    # Native pattern selection/rate commands have not yet been verified.
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = None
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
        if self._state.color_id in COLOR_BY_ID:
            return ColorMode.RGB
        if self._state.color_id in PATTERN_BY_ID:
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
        return PATTERN_BY_ID.get(self._state.color_id)

    async def async_request_call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Trace HA's service wait without changing its semaphore or inputs.

        Older HA single-entity calls bypass this hook; the coordinator creates
        their action trace when the entity method is reached instead.
        """
        trace_id = new_request_trace()
        started = time.monotonic()
        _LOGGER.debug(
            "Haven action: phase=service_received trace=%s target=%s",
            trace_id, target_reference(self.coordinator.account_id, self._key),
        )
        with request_trace(trace_id):
            try:
                return await super().async_request_call(coro)
            finally:
                # Completion means the caller finished, including cancellation
                # or an error; only phase=accepted reports a successful batch.
                _LOGGER.debug(
                    "Haven action: phase=service_finished trace=%s elapsed=%.3fs",
                    trace_id, time.monotonic() - started,
                )

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
            if effect in HAVEN_PATTERN_MAP:
                raise HomeAssistantError(
                    "Native Haven pattern controls are not supported yet; use the Haven app"
                )
            if effect not in HAVEN_COLOR_MAP:
                raise HomeAssistantError(f"Unknown Haven color preset: {effect}")
            # Compatibility for old effect: Fire/Sunset/Ocean actions, and
            # exact routing for the named Color preset selector.
            color_id = HAVEN_COLOR_MAP[effect]
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            color_id = min(HAVEN_KELVIN_MAP, key=lambda value: abs(value - kelvin))
            color_id = HAVEN_KELVIN_MAP[color_id]
        elif ATTR_RGB_COLOR in kwargs:
            if tuple(kwargs[ATTR_RGB_COLOR]) == (255, 255, 255):
                # RGB white has no Kelvin value. Resolve a real white preset
                # under the operation lock: retain the current white setting,
                # or use warm white when coming from another preset.
                commands.append(("set_white", ()))
            else:
                color_id = self._find_closest_color_id(*kwargs[ATTR_RGB_COLOR])
        elif effect == EFFECT_OFF:
            # Preserve the old alias reset at execution time; this is not a
            # command that stops a native Haven pattern.
            commands.append(("reset_legacy_color", ()))

        if color_id is not None:
            commands.append(("set_color", (color_id,)))
        # Cached power can lag the app or a schedule. Brightness implies on;
        # other turn_on requests must explicitly power the target on.
        if ATTR_BRIGHTNESS not in kwargs:
            commands.append(("turn_on", ()))
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Haven color configuration: target=%s supported_modes=%s white_kelvin=%s "
                "capability_source=inherited_presets",
                target_reference(self.coordinator.account_id, self._key),
                sorted(mode.value for mode in self.supported_color_modes),
                sorted(HAVEN_KELVIN_MAP),
            )
        await self.coordinator.async_send_commands(
            self._key, commands,
            requested=action_description(kwargs, getattr(self, "_context", None)),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_send_commands(
            self._key, [("turn_off", ())],
            requested=action_description(kwargs, getattr(self, "_context", None), turn_off=True),
        )

    @staticmethod
    def _find_closest_color_id(r: int, g: int, b: int) -> int:
        rgb = min(
            HAVEN_RGB_MAP,
            key=lambda value: (r - value[0]) ** 2 + (g - value[1]) ** 2 + (b - value[2]) ** 2,
        )
        return HAVEN_RGB_MAP[rgb]
