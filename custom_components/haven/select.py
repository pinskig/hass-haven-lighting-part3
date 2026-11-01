"""Exact white temperature presets for existing Haven lights."""

from __future__ import annotations

from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN, DOMAIN as LIGHT_DOMAIN
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting.colors import HAVEN_KELVIN_MAP, KELVIN_BY_ID
from .havenlighting.models import TargetKey
from .registry import async_prepare_registries, entity_unique_id

WHITE_TEMPERATURE_OPTIONS = {f"{kelvin} K": kelvin for kelvin in HAVEN_KELVIN_MAP}

# Keep HA's relative select_next/select_previous actions serialized as well.
# The delegated light service retains the existing light platform semaphore.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a companion selector when each zone or group is discovered."""
    coordinators: list[HavenCoordinator] = hass.data[DOMAIN][config_entry.entry_id]["coordinators"]
    warned: set[str] = set()

    def watch_inventory(coordinator: HavenCoordinator) -> None:
        known: set[TargetKey] = set()

        @callback
        def add_new_entities() -> None:
            if not coordinator.last_update_success:
                return
            added = (coordinator.data or {}).keys() - known
            if not added:
                return
            async_prepare_registries(hass, config_entry, coordinators, warned)
            keys = sorted(added, key=lambda key: (key.kind, key.remote_id))
            entities = [HavenWhiteTemperatureSelect(key, coordinator) for key in keys]
            known.update(keys)
            async_add_entities(entities)

        config_entry.async_on_unload(coordinator.async_add_listener(add_new_entities))
        add_new_entities()

    for coordinator in coordinators:
        watch_inventory(coordinator)


class HavenWhiteTemperatureSelect(CoordinatorEntity[HavenCoordinator], SelectEntity):
    """Select a known white preset using the associated light's service path."""

    _attr_has_entity_name = True
    _attr_translation_key = "white_temperature"
    _attr_options = list(WHITE_TEMPERATURE_OPTIONS)

    def __init__(self, key: TargetKey, coordinator: HavenCoordinator) -> None:
        super().__init__(coordinator)
        self._key = key
        self._last_name = coordinator.data[key].name
        self._light_unique_id = entity_unique_id(coordinator.account_id, key)
        self._attr_unique_id = f"{self._light_unique_id}:white_temperature"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._light_unique_id)},
            name=self._last_name,
            manufacturer="Haven",
            model=f"Haven {key.kind}",
        )

    @property
    def available(self) -> bool:
        return super().available and self._key in (self.coordinator.data or {})

    @property
    def current_option(self) -> str | None:
        state = (self.coordinator.data or {}).get(self._key)
        kelvin = KELVIN_BY_ID.get(state.color_id) if state is not None else None
        return f"{kelvin} K" if kelvin is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        if updated := (self.coordinator.data or {}).get(self._key):
            if updated.name != self._last_name:
                self._attr_device_info = {**self._attr_device_info, "name": updated.name}
                if self.registry_entry is not None and self.registry_entry.device_id:
                    dr.async_get(self.hass).async_update_device(
                        self.registry_entry.device_id, name=updated.name
                    )
                self._last_name = updated.name
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Select the white preset and turn on through the existing light entity."""
        if option not in WHITE_TEMPERATURE_OPTIONS:
            raise HomeAssistantError("Unknown Haven white temperature preset")
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, self._light_unique_id)
        linked = registry.async_get(entity_id) if entity_id is not None else None
        if linked is None:
            raise HomeAssistantError("The corresponding Haven light entity is missing")
        if linked.disabled_by is not None:
            raise HomeAssistantError(
                "Enable the corresponding Haven light entity to select a white temperature"
            )
        state = self.hass.states.get(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            raise HomeAssistantError("The corresponding Haven light entity is unavailable")
        # Resolve by unique ID on every call so user entity_id changes are safe.
        # Delegation also lets HA resolve later toggle/brightness-step actions
        # after this command, using the light platform's existing serialization.
        await self.hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {
                ATTR_ENTITY_ID: entity_id,
                ATTR_COLOR_TEMP_KELVIN: WHITE_TEMPERATURE_OPTIONS[option],
            },
            blocking=True,
            context=getattr(self, "_context", None),
        )
