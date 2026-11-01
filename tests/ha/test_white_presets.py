"""Exact white selectors exercised through real HA services and registries."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import threading

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import ACCOUNT_ID, LOCATION_ID, async_drain_scheduled_work
from test_entities import entity_for
from test_request_budgets import (
    budget_transport,
    gate_clock,
    hass_config_dir,
    hass_storage,
    real_clients,
    real_loaded_entry,
)

WHITE_PRESETS = [2700, 3000, 3500, 3700, 4000, 4400, 4700, 5000]


def select_for(hass, *, kind="zone", remote_id=101):
    unique = f"haven:{ACCOUNT_ID}:{LOCATION_ID}:{kind}:{remote_id}:white_temperature"
    result = er.async_get(hass).async_get_entity_id("select", "haven", unique)
    assert result is not None, unique
    return result


async def select_white(hass, kelvin, *, entity_id=None, context=None):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": entity_id or select_for(hass), "option": f"{kelvin} K"},
        blocking=True, context=context,
    )


@pytest.mark.parametrize(("kelvin", "color_id"), list(zip(WHITE_PRESETS, range(1, 9))))
async def test_exact_white_options_send_preset_and_on(
    hass, loaded_entry, haven_api, kelvin, color_id
):
    selector = select_for(hass)
    light = entity_for(hass)
    registry = er.async_get(hass)
    original_light = registry.async_get(light)
    assert registry.async_get(selector).device_id == original_light.device_id
    assert hass.states.get(selector).name == "Front yard White temperature"
    assert hass.states.get(selector).attributes["options"] == [f"{value} K" for value in WHITE_PRESETS]
    assert hass.states.get(light).state == "off"
    polls_before = len(haven_api.poll_calls)

    # Selecting the already-reported 2700 K must still power an off light on.
    await select_white(hass, kelvin)
    assert [(path, kwargs["json"]) for _method, path, kwargs in haven_api.command_calls] == [
        ("/Commands/SetColor", {"id": 101, "type": "Zone", "colorId": color_id}),
        ("/Commands/On", {"id": 101, "type": "Zone"}),
    ]
    assert hass.states.get(selector).state == f"{kelvin} K"
    state = hass.states.get(light)
    assert state.state == "on"
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == kelvin
    assert state.attributes["brightness"] == 128
    assert registry.async_get(light).id == original_light.id
    assert len(haven_api.poll_calls) == polls_before


@pytest.mark.parametrize("initial_color", [11, 12], ids=["red", "fire"])
async def test_2700_preset_replaces_active_red_or_fire(
    hass, loaded_entry, haven_api, initial_color
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.zones[0].update(isOn=True, colorId=initial_color)
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert hass.states.get(select_for(hass)).state == "unknown"

    await select_white(hass, 2700)
    assert [(path, kwargs["json"]) for _method, path, kwargs in haven_api.command_calls] == [
        ("/Commands/SetColor", {"id": 101, "type": "Zone", "colorId": 1}),
        ("/Commands/On", {"id": 101, "type": "Zone"}),
    ]
    state = hass.states.get(entity_for(hass))
    assert state.state == "on"
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 2700
    assert state.attributes["effect"] is None
    assert hass.states.get(select_for(hass)).state == "2700 K"


async def test_group_inventory_reappearance_and_unload_keep_selector_identity(
    hass, loaded_entry, haven_api
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.groups = [haven_api.group(101, "Group with the same number")]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    zone_selector = select_for(hass)
    group_selector = select_for(hass, kind="group")
    registry = er.async_get(hass)
    original = registry.async_get(group_selector)
    assert original.device_id == registry.async_get(entity_for(hass, kind="group")).device_id
    assert original.device_id != registry.async_get(zone_selector).device_id

    await select_white(hass, 4400, entity_id=group_selector)
    assert [kwargs["json"] for _method, _path, kwargs in haven_api.command_calls] == [
        {"id": 101, "type": "Group", "colorId": 6}, {"id": 101, "type": "Group"},
    ]
    assert haven_api.zones[0]["colorId"] == 1
    listeners = len(coordinator._listeners)
    haven_api.groups = []
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert hass.states.get(group_selector).state == "unavailable"

    haven_api.groups = [haven_api.group(101, "Renamed group", color=6)]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert select_for(hass, kind="group") == group_selector
    assert registry.async_get(group_selector).id == original.id
    assert hass.states.get(group_selector).state == "4400 K"
    assert "Renamed group" in hass.states.get(group_selector).name
    assert len(coordinator._listeners) == listeners

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await async_drain_scheduled_work(hass)
    assert not coordinator._listeners
    before = len(haven_api.calls)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await async_drain_scheduled_work(hass)
    assert len(haven_api.calls) == before
    assert await hass.config_entries.async_setup(loaded_entry.entry_id)
    await async_drain_scheduled_work(hass)
    replacement = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    assert len(replacement._listeners) == listeners
    assert registry.async_get(select_for(hass, kind="group")).id == original.id


async def test_selector_follows_external_color_and_white_snapshots(
    hass, loaded_entry, haven_api
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    selector = select_for(hass)
    for color_id in (25, 12, 999, None):
        haven_api.zones[0].update(colorId=color_id, isOn=True)
        await coordinator.async_refresh()
        await async_drain_scheduled_work(hass)
        assert hass.states.get(selector).state == "unknown"
    haven_api.zones[0].update(colorId=6, isOn=False)
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert hass.states.get(selector).state == "4400 K"
    assert hass.states.get(entity_for(hass)).state == "off"
    assert haven_api.command_calls == []


async def test_selector_resolves_renamed_light_and_preserves_context(
    hass, loaded_entry, haven_api, monkeypatch
):
    from custom_components.haven.light import HavenLight

    registry = er.async_get(hass)
    registry.async_update_entity(entity_for(hass), new_entity_id="light.renamed_haven_target")
    await async_drain_scheduled_work(hass)
    context = Context(parent_id=Context().id)
    received = []
    turn_on = HavenLight.async_turn_on

    async def observe(entity, **kwargs):
        received.append((entity._context, dict(kwargs)))
        await turn_on(entity, **kwargs)

    monkeypatch.setattr(HavenLight, "async_turn_on", observe)
    await select_white(hass, 3500, context=context)
    assert len(received) == 1
    assert received[0][0] is context
    # Older HA also supplies its derived mired alias alongside Kelvin.
    assert received[0][1]["color_temp_kelvin"] == 3500
    assert "rgb_color" not in received[0][1]
    assert hass.states.get("light.renamed_haven_target").attributes["color_temp_kelvin"] == 3500
    assert haven_api.zones[0]["colorId"] == 3


@pytest.mark.parametrize("missing", [False, True], ids=["disabled", "missing"])
async def test_selector_reports_disabled_or_missing_linked_light(
    hass, loaded_entry, haven_api, missing
):
    registry = er.async_get(hass)
    entity_id = entity_for(hass)
    if missing:
        registry.async_remove(entity_id)
        message = "light entity is missing"
    else:
        registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
        message = "Enable the corresponding Haven light entity"
    await async_drain_scheduled_work(hass)
    with pytest.raises(HomeAssistantError, match=message):
        await select_white(hass, 3500)
    assert haven_api.command_calls == []
    assert hass.states.get(select_for(hass)).state == "2700 K"


async def test_invalid_white_option_sends_no_command(hass, loaded_entry, haven_api):
    with pytest.raises(HomeAssistantError):
        await select_white(hass, 3100)
    assert haven_api.command_calls == []
    assert hass.states.get(select_for(hass)).state == "2700 K"


@pytest.mark.parametrize("status", [400, 429])
async def test_rejected_white_selection_preserves_state_and_shared_request_gate(
    hass, real_loaded_entry, haven_api, budget_transport, status
):
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    haven_api.zones[0].update(isOn=True, colorId=25)
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    budget_transport.command_status = status
    before = len(budget_transport.calls)
    with pytest.raises(HomeAssistantError):
        await select_white(hass, 3500)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"),
    ]
    assert hass.states.get(select_for(hass)).state == "unknown"
    assert hass.states.get(entity_for(hass)).attributes["rgb_color"] == (0, 0, 255)
    if status == 429:
        assert real_loaded_entry.gate.rate_limit_remaining == 3600
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "light", "turn_off", {"entity_id": entity_for(hass)}, blocking=True
            )
        assert len(budget_transport.calls) == before + 1


async def test_empty_acknowledgments_reuse_client_and_one_shared_refresh(
    hass, real_loaded_entry, haven_api, budget_transport, real_clients
):
    budget_transport.command_response_body = b""
    before = len(budget_transport.calls)
    polls_before = len(haven_api.poll_calls)
    await select_white(hass, 3500)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_for(hass), "brightness": 204}, blocking=True
    )
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
        ("POST", "/Commands/Brightness"),
    ]
    assert len(real_clients.clients) == 1
    assert all(session is real_loaded_entry.client._credentials._session
               for session, _method, _path in budget_transport.calls[before:])
    assert len(haven_api.poll_calls) == polls_before
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await async_drain_scheduled_work(hass)
    assert len(haven_api.poll_calls) == polls_before + 2
    assert hass.states.get(select_for(hass)).state == "3500 K"
    assert hass.states.get(entity_for(hass)).attributes["brightness"] == 204


@pytest.mark.skipif(
    HA_VERSION.startswith("2024.1."),
    reason="HA 2024.1 bypasses the platform semaphore for single-entity services",
)
async def test_toggle_waits_for_in_flight_white_selection(
    hass, loaded_entry, haven_api, monkeypatch
):
    from custom_components.haven.light import HavenLight

    in_flight = asyncio.Event()
    toggle_arrived = asyncio.Event()
    release = threading.Event()
    request = haven_api.make_request
    request_call = HavenLight.async_request_call
    received = 0
    tasks = []

    def hold_color_reply(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if path == "/Commands/SetColor":
            hass.loop.call_soon_threadsafe(in_flight.set)
            if not release.wait(10):
                raise AssertionError("The test did not release the selected white reply")
        return result

    async def observe_request(entity, coro):
        nonlocal received
        received += 1
        if received == 2:
            toggle_arrived.set()
        return await request_call(entity, coro)

    monkeypatch.setattr(haven_api, "make_request", hold_color_reply)
    monkeypatch.setattr(HavenLight, "async_request_call", observe_request)
    assert hass.states.get(entity_for(hass)).state == "off"
    try:
        tasks.append(asyncio.create_task(select_white(hass, 3500)))
        await asyncio.wait_for(in_flight.wait(), 5)
        tasks.append(asyncio.create_task(hass.services.async_call(
            "light", "toggle", {"entity_id": entity_for(hass)}, blocking=True,
        )))
        await asyncio.wait_for(toggle_arrived.wait(), 5)
        assert [path for _method, path, _kwargs in haven_api.command_calls] == ["/Commands/SetColor"]
    finally:
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert outcomes == [None, None]
    assert [path for _method, path, _kwargs in haven_api.command_calls] == [
        "/Commands/SetColor", "/Commands/On", "/Commands/Off",
    ]
    assert hass.states.get(entity_for(hass)).state == "off"
    assert hass.states.get(select_for(hass)).state == "3500 K"
