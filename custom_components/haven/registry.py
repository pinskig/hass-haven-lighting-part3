"""Migrate legacy registry identities without deleting users' entries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting.models import LightState, TargetKey

_LOGGER = logging.getLogger(__name__)
_LEGACY_PREFIX = "haven_light_"


def entity_unique_id(account_id: str, key: TargetKey) -> str:
    return f"haven:{account_id}:{key.location_id}:{key.kind.lower()}:{key.remote_id}"


def location_identifier(account_id: str, location_id: int) -> str:
    return f"haven:{account_id}:location:{location_id}"


def _exclusively_owned(device, entry_id: str) -> bool:
    # HA 2026.10 has single-owner devices; older releases use a set.
    if hasattr(device, "config_entry_id"):
        return device.config_entry_id == entry_id
    return device.config_entries == {entry_id}


@callback
def async_prepare_registries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinators: list[HavenCoordinator],
    warned: set[str],
) -> None:
    """Preserve registry IDs and customization where legacy identity is known.

    Missing targets stay in the registry. Ambiguous legacy identities stay
    unavailable instead of being silently redirected to a different target.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    registered = dr.async_entries_for_config_entry(devices, entry.entry_id)
    by_identifier = {
        identifier: device
        for device in registered
        for identifier in device.identifiers
    }
    wanted_identifiers = {
        (DOMAIN, location_identifier(coordinator.account_id, coordinator.location.location_id))
        for coordinator in coordinators
    } | {
        (DOMAIN, entity_unique_id(coordinator.account_id, key))
        for coordinator in coordinators
        for key in (coordinator.data or {})
    }
    # Older HA registries match identifiers globally. Do not let get_or_create
    # attach this entry to, or change, another entry's namespaced device.
    all_devices = devices.devices
    # Older HA exposes a mapping; current HA iterates device entries directly.
    if isinstance(all_devices, Mapping):
        all_devices = all_devices.values()
    for device in all_devices:
        if device.identifiers & wanted_identifiers and not _exclusively_owned(device, entry.entry_id):
            raise ConfigEntryError(
                "Another entry already owns these Haven devices; resolve the duplicate account entry"
            )
    for coordinator in coordinators:
        for key in (coordinator.data or {}):
            unique_id = entity_unique_id(coordinator.account_id, key)
            registered_id = entities.async_get_entity_id("light", DOMAIN, unique_id)
            if registered_id is not None:
                registered_entity = entities.async_get(registered_id)
                if registered_entity.config_entry_id not in (None, entry.entry_id):
                    raise ConfigEntryError(
                        "Another entry already owns these Haven entities; resolve the duplicate account entry"
                    )
    parents: dict[int, str] = {}
    parent_locations: dict[str, int] = {}
    targets: dict[TargetKey, tuple[HavenCoordinator, LightState]] = {}
    by_remote_id: dict[int, list[TargetKey]] = defaultdict(list)

    for coordinator in coordinators:
        location = coordinator.location
        identifier = (DOMAIN, location_identifier(coordinator.account_id, location.location_id))
        parent = by_identifier.get(identifier)
        legacy = by_identifier.get((DOMAIN, str(location.location_id)))
        if (
            parent is None
            and legacy is not None
            and legacy.model == "Haven Controller/Location"
            and _exclusively_owned(legacy, entry.entry_id)
        ):
            parent = devices.async_update_device(
                legacy.id,
                new_identifiers=(legacy.identifiers - {(DOMAIN, str(location.location_id))})
                | {identifier},
            )
        if parent is None:
            parent = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={identifier},
                manufacturer="Haven",
                name=location.name,
                model="Haven Controller/Location",
            )
        parents[location.location_id] = parent.id
        parent_locations[parent.id] = location.location_id
        if legacy is not None and legacy.model == "Haven Controller/Location":
            parent_locations[legacy.id] = location.location_id
        for key, state in (coordinator.data or {}).items():
            targets[key] = (coordinator, state)
            by_remote_id[key.remote_id].append(key)

    # Plan entity migrations before claiming devices, using both the recorded
    # kind and parent when a raw number appears in more than one namespace.
    migrations = {}
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.domain != "light" or not entity.unique_id.startswith(_LEGACY_PREFIX):
            continue
        raw_id = entity.unique_id[len(_LEGACY_PREFIX):]
        if not raw_id.isascii() or not raw_id.isdecimal():
            continue
        candidates = list(by_remote_id.get(int(raw_id), []))
        if not candidates:
            continue
        legacy_device = devices.async_get(entity.device_id) if entity.device_id else None
        kinds = {"Haven Zone": "Zone", "Haven Group": "Group"}
        kind = kinds.get(legacy_device.model) if legacy_device else None
        location_id = None
        if legacy_device is not None and legacy_device.via_device_id:
            location_id = parent_locations.get(legacy_device.via_device_id)
            # A known parent can be absent from today's API response. Preserve
            # that evidence instead of assigning its target to another location.
            parent = devices.async_get(legacy_device.via_device_id)
            if location_id is None and parent is not None and parent.model == "Haven Controller/Location":
                parent_ids = set()
                for domain, value in parent.identifiers:
                    if domain != DOMAIN:
                        continue
                    if value.isascii() and value.isdecimal() and int(value) > 0:
                        parent_ids.add(int(value))
                    for coordinator in coordinators:
                        prefix = f"haven:{coordinator.account_id}:location:"
                        suffix = value.removeprefix(prefix)
                        if value.startswith(prefix) and suffix.isascii() and suffix.isdecimal() and int(suffix) > 0:
                            parent_ids.add(int(suffix))
                if len(parent_ids) == 1:
                    location_id = parent_ids.pop()
        if kind is None or location_id is None or not _exclusively_owned(legacy_device, entry.entry_id):
            if entity.entity_id not in warned:
                _LOGGER.warning(
                    "Cannot safely migrate %s: its legacy kind, parent, or ownership is unknown. "
                    "The old entity is retained; use the separately discovered target",
                    entity.entity_id,
                )
                warned.add(entity.entity_id)
            continue
        candidates = [
            key for key in candidates if key.kind == kind and key.location_id == location_id
        ]
        if len(candidates) != 1:
            if len(candidates) > 1 and entity.entity_id not in warned:
                _LOGGER.warning(
                    "Cannot safely migrate %s: several Haven targets share its legacy ID. "
                    "The old entity is retained; use the separate newly discovered targets",
                    entity.entity_id,
                )
                warned.add(entity.entity_id)
            continue
        key = candidates[0]
        coordinator, _state = targets[key]
        unique_id = entity_unique_id(coordinator.account_id, key)
        if not legacy_device.identifiers & {
            (DOMAIN, str(key.remote_id)), (DOMAIN, unique_id)
        }:
            if entity.entity_id not in warned:
                _LOGGER.warning(
                    "Retaining legacy %s because its device identifier does not match the target; "
                    "automatic migration would change device automation references",
                    entity.entity_id,
                )
                warned.add(entity.entity_id)
            continue
        existing_device = by_identifier.get((DOMAIN, unique_id))
        if existing_device is not None and existing_device.id != legacy_device.id:
            if entity.entity_id not in warned:
                _LOGGER.warning(
                    "Retaining legacy %s because its Haven target already has a different device entry; "
                    "automatic migration would change device automation references",
                    entity.entity_id,
                )
                warned.add(entity.entity_id)
            continue
        existing_id = entities.async_get_entity_id("light", DOMAIN, unique_id)
        if existing_id is not None and existing_id != entity.entity_id:
            if entity.entity_id not in warned:
                _LOGGER.warning(
                    "Retaining legacy %s because %s already owns its Haven target",
                    entity.entity_id, existing_id,
                )
                warned.add(entity.entity_id)
            continue
        migrations[key] = (entity, legacy_device)

    claimed_devices = set(parents.values())
    for key, (coordinator, state) in targets.items():
        unique_id = entity_unique_id(coordinator.account_id, key)
        identifier = (DOMAIN, unique_id)
        device = by_identifier.get(identifier)
        migration = migrations.get(key)
        legacy = migration[1] if migration else None
        if (
            device is None
            and legacy is not None
            and legacy.id not in claimed_devices
            and (DOMAIN, str(key.remote_id)) in legacy.identifiers
            and _exclusively_owned(legacy, entry.entry_id)
        ):
            device = devices.async_update_device(
                legacy.id,
                new_identifiers=(legacy.identifiers - {(DOMAIN, str(key.remote_id))})
                | {identifier},
                via_device_id=parents[key.location_id],
            )
        if device is None:
            device = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={identifier},
                manufacturer="Haven",
                model=f"Haven {key.kind}",
                name=state.name,
            )
        claimed_devices.add(device.id)
        # Both minimum and current HA support via_device_id on updates. This
        # avoids the old identifier-only via_device lookup across accounts.
        devices.async_update_device(device.id, via_device_id=parents[key.location_id])
        if migration is not None:
            entity, _legacy = migration
            entities.async_update_entity(
                entity.entity_id, new_unique_id=unique_id, device_id=device.id
            )
