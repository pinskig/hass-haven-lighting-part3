"""Exercise real HA service routing, debouncing, config flows, and unloads."""

from datetime import timedelta

import pytest

from homeassistant import config_entries
from homeassistant.helpers.debounce import Debouncer
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import async_drain_scheduled_work
from test_entities import entity_for


async def test_real_debouncer_coalesces_command_burst(hass, loaded_entry, haven_api):
    from custom_components.haven.const import COMMAND_REFRESH_DELAY

    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    assert isinstance(coordinator._debounced_refresh, Debouncer)
    before = len(haven_api.poll_calls)
    entity_id = entity_for(hass)
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "brightness": 128, "rgb_color": (0, 0, 255)},
        blocking=True,
    )
    await hass.services.async_call("light", "turn_off", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert len(haven_api.poll_calls) == before

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=COMMAND_REFRESH_DELAY + 1)
    )
    await async_drain_scheduled_work(hass)
    assert len(haven_api.poll_calls) == before + 2
    assert hass.states.get(entity_id).state == "off"


async def test_color_only_turn_on_explicitly_powers_an_off_light(
    hass, loaded_entry, haven_api
):
    entity_id = entity_for(hass)
    assert hass.states.get(entity_id).state == "off"
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)}, blocking=True
    )
    commands = [call[1] for call in haven_api.command_calls]
    assert "/Commands/On" in commands
    assert "/Commands/SetColor" in commands
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["rgb_color"] == (0, 0, 255)


@pytest.mark.parametrize(
    ("parameters", "color_id"),
    [
        ({"rgb_color": (0, 0, 255)}, 25),
        ({"color_temp_kelvin": 3500}, 3),
        ({"effect": "Fire"}, 12),
    ],
)
async def test_color_turn_on_powers_device_despite_stale_cached_on_state(
    hass, loaded_entry, haven_api, parameters, color_id
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.zones[0]["isOn"] = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entity_id = entity_for(hass)
    assert hass.states.get(entity_id).state == "on"

    # The app or a schedule switches it off between coordinator polls.
    haven_api.zones[0]["isOn"] = False
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, **parameters}, blocking=True
    )
    assert haven_api.zones[0]["isOn"] is True
    assert haven_api.zones[0]["colorId"] == color_id
    assert [call[1] for call in haven_api.command_calls] == [
        "/Commands/SetColor", "/Commands/On"
    ]
    assert hass.states.get(entity_id).state == "on"

    # Confirm the requested power persists when optimistic state is refreshed.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"


@pytest.mark.parametrize("brightness", [128, 0])
async def test_brightness_power_actions_do_not_add_redundant_on(
    hass, loaded_entry, haven_api, brightness
):
    entity_id = entity_for(hass)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "brightness": brightness}, blocking=True
    )
    expected = "/Commands/Brightness" if brightness else "/Commands/Off"
    assert [call[1] for call in haven_api.command_calls] == [expected]
    assert haven_api.zones[0]["isOn"] is bool(brightness)
    assert hass.states.get(entity_id).state == ("on" if brightness else "off")


async def test_unload_closes_client_cancels_timers_and_retains_account_budget(
    hass, loaded_entry, haven_api, client_factory
):
    from custom_components.haven.runtime import async_get_account_store

    accounts = await async_get_account_store(hass)
    gate = await accounts.async_get_gate(loaded_entry.data["email"])
    old_client = client_factory.clients[-1]
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_for(hass)}, blocking=True
    )
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    before = len(haven_api.calls)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await async_drain_scheduled_work(hass)
    assert len(haven_api.calls) == before
    old_client.close.assert_called_once_with()
    assert loaded_entry.entry_id not in hass.data.get("haven", {})
    assert await accounts.async_get_gate(loaded_entry.data["email"]) is gate


async def test_real_reauth_flow_updates_password_and_reloads_same_entry(
    hass, loaded_entry, client_factory
):
    original = client_factory.clients[0]
    result = await hass.config_entries.flow.async_init(
        "haven",
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": loaded_entry.entry_id},
        data=loaded_entry.data,
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"password": "replacement-password"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert loaded_entry.data["password"] == "replacement-password"
    assert len(hass.config_entries.async_entries("haven")) == 1
    assert client_factory.clients[-1] is not original
    original.close.assert_called_once_with()
    assert all(client.request_gate is original.request_gate for client in client_factory.clients)
    assert hass.states.get(entity_for(hass)) is not None
