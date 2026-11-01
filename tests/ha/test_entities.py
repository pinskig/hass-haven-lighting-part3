"""Entity, inventory, and migration tests executed by the real HA platform."""

from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import ACCOUNT_ID, LOCATION_ID, async_drain_scheduled_work


def entity_for(hass, *, kind="zone", remote_id=101):
    unique = f"haven:{ACCOUNT_ID}:{LOCATION_ID}:{kind}:{remote_id}"
    result = er.async_get(hass).async_get_entity_id("light", "haven", unique)
    assert result is not None, unique
    return result


async def test_real_setup_preserves_six_digit_controller(hass, loaded_entry):
    registry = dr.async_get(hass)
    controller = next(
        (device for device in dr.async_entries_for_config_entry(registry, loaded_entry.entry_id)
         if ("haven", f"haven:{ACCOUNT_ID}:location:{LOCATION_ID}") in device.identifiers),
        None,
    )
    assert controller is not None
    entity = er.async_get(hass).async_get(entity_for(hass))
    child = registry.async_get(entity.device_id)
    assert child.via_device_id == controller.id
    assert hass.states.get(entity.entity_id).state == "off"


async def test_same_number_zone_and_group_get_distinct_entities(
    hass, entry, client_factory, haven_api
):
    haven_api.groups = [haven_api.group(101, "Overlapping group")]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    zone = entity_for(hass)
    group = entity_for(hass, kind="group")
    assert zone != group
    assert er.async_get(hass).async_get(zone).device_id != er.async_get(hass).async_get(group).device_id
    await hass.services.async_call("light", "turn_on", {"entity_id": group}, blocking=True)
    assert haven_api.command_calls[-1][2]["json"]["type"] == "Group"
    assert haven_api.zones[0]["isOn"] is False


async def test_color_and_effect_state_follow_server_snapshot(
    hass, loaded_entry, haven_api
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.zones[0].update(isOn=True, colorId=1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_for(hass))
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 2700

    haven_api.zones[0]["colorId"] = 25
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_for(hass))
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] == (0, 0, 255)

    haven_api.zones[0]["colorId"] = 12
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_for(hass)).attributes["effect"] == "Fire"


async def test_inventory_add_remove_return_and_rename(hass, loaded_entry, haven_api):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    original_id = entity_for(hass)
    original_registry = er.async_get(hass).async_get(original_id)
    haven_api.zones.append(haven_api.zone(102, "New zone"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_for(hass, remote_id=102)) is not None

    haven_api.zones = [record for record in haven_api.zones if record["id"] != 101]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(original_id).state == "unavailable"
    assert er.async_get(hass).async_get(original_id).id == original_registry.id

    haven_api.zones.append(haven_api.zone(101, "Renamed zone", is_on=True))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(original_id)
    assert state.state == "on"
    assert "Renamed zone" in state.name
    assert er.async_get(hass).async_get(original_id).id == original_registry.id


async def test_empty_initial_inventory_still_discovers_later_targets(
    hass, entry, client_factory, haven_api
):
    haven_api.zones = []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    haven_api.zones.append(haven_api.zone(103, "First zone"))
    # With no entities, only the platform inventory listener can keep the real
    # coordinator timer alive. Exercise that timer instead of forcing a refresh.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await async_drain_scheduled_work(hass)
    assert hass.states.get(entity_for(hass, remote_id=103)) is not None


async def test_immutable_snapshots_suppress_only_unchanged_updates(
    hass, loaded_entry, haven_api
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    previous = coordinator.data
    key = next(iter(previous))
    observer = Mock()
    remove = coordinator.async_add_listener(observer)
    try:
        await coordinator.async_refresh()
        assert coordinator.data == previous
        observer.assert_not_called()
        haven_api.zones[0]["isOn"] = True
        await coordinator.async_refresh()
        assert previous[key].is_on is False
        assert coordinator.data[key].is_on is True
        observer.assert_called_once()
        with pytest.raises(FrozenInstanceError):
            previous[key].is_on = True
    finally:
        remove()


async def test_legacy_migration_keeps_entity_id_device_id_and_user_metadata(
    hass, entry, client_factory
):
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    area = ar.async_get(hass).async_create("Garden")
    controller = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("haven", str(LOCATION_ID))},
        name="Existing location",
        manufacturer="Haven",
        model="Haven Controller/Location",
    )
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("haven", "101")},
        name="Existing device",
        manufacturer="Haven",
        model="Haven Zone",
    )
    devices.async_update_device(
        old_device.id, area_id=area.id, name_by_user="My garden light", via_device_id=controller.id
    )
    old_entity = entities.async_get_or_create(
        "light", "haven", "haven_light_101",
        config_entry=entry,
        device_id=old_device.id,
        suggested_object_id="preserve_this_entity_id",
    )
    entities.async_update_entity(old_entity.entity_id, name="Custom entity name")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    migrated = entities.async_get(entity_for(hass))
    assert migrated.entity_id == old_entity.entity_id
    assert migrated.device_id == old_device.id
    assert migrated.name == "Custom entity name"
    migrated_device = devices.async_get(old_device.id)
    assert migrated_device.name_by_user == "My garden light"
    assert migrated_device.area_id == area.id
    assert migrated_device.via_device_id == controller.id
    assert devices.async_get(controller.id) is not None


async def test_ambiguous_legacy_identity_is_retained_without_guessing(
    hass, entry, client_factory, haven_api
):
    haven_api.groups = [haven_api.group(101, "Same-number group")]
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("haven", "101")},
        name="Ambiguous old device",
        manufacturer="Haven",
        model="Haven",
    )
    old_entity = entities.async_get_or_create(
        "light", "haven", "haven_light_101",
        config_entry=entry, device_id=old_device.id,
        suggested_object_id="keep_ambiguous_entity",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entities.async_get(old_entity.entity_id).unique_id == "haven_light_101"
    assert devices.async_get(old_device.id) is not None
    assert entity_for(hass) != old_entity.entity_id
    assert entity_for(hass, kind="group") != old_entity.entity_id


async def test_initially_missing_legacy_target_migrates_when_it_returns(
    hass, entry, client_factory, haven_api
):
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    controller = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("haven", str(LOCATION_ID))},
        name="Existing location", manufacturer="Haven", model="Haven Controller/Location",
    )
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("haven", "101")},
        name="Temporarily absent zone", manufacturer="Haven", model="Haven Zone",
    )
    devices.async_update_device(old_device.id, via_device_id=controller.id)
    old_entity = entities.async_get_or_create(
        "light", "haven", "haven_light_101",
        config_entry=entry, device_id=old_device.id,
        suggested_object_id="preserve_missing_zone",
    )
    haven_api.zones = []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entities.async_get(old_entity.entity_id) is not None
    assert devices.async_get(old_device.id) is not None

    haven_api.zones = [haven_api.zone(101, "Returned zone")]
    coordinator = hass.data["haven"][entry.entry_id]["coordinators"][0]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert entity_for(hass) == old_entity.entity_id
    assert entities.async_get(old_entity.entity_id).device_id == old_device.id
    assert hass.states.get(old_entity.entity_id).state == "off"
