"""Color service ordering, preset stability, and reconciliation in real HA."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import threading
from urllib.parse import urlsplit

import pytest
import requests
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.exceptions import HomeAssistantError

from conftest import async_drain_scheduled_work
from test_entities import entity_for
from test_request_budgets import (  # Reuse the real client and offline HTTP boundary.
    budget_transport,
    gate_clock,
    hass_config_dir,
    hass_storage,
    real_clients,
    real_loaded_entry,
)


def _commands(haven_api):
    return [
        (path, (kwargs["json"]["type"], kwargs["json"]["id"]),
         kwargs["json"].get("colorId", kwargs["json"].get("brightness")))
        for _method, path, kwargs in haven_api.command_calls
    ]


def _color_commands(color_id, target=("Zone", 101)):
    return [("/Commands/SetColor", target, color_id), ("/Commands/On", target, None)]


@pytest.fixture
def queued_services(hass, haven_api, monkeypatch):
    """Hold the first color reply until every service reaches HA's call boundary."""
    from custom_components.haven.light import HavenLight

    async def run(calls):
        in_flight = asyncio.Event()
        all_arrived = asyncio.Event()
        release = threading.Event()
        request = haven_api.make_request
        request_call = HavenLight.async_request_call
        turn_on = HavenLight.async_turn_on
        turn_off = HavenLight.async_turn_off
        observed = ContextVar("color_test_observed_service", default=False)
        arrived = 0
        held = False
        tasks = []
        before = len(haven_api.command_calls)

        def hold_first_reply(method, path, **kwargs):
            nonlocal held
            result = request(method, path, **kwargs)
            if path == "/Commands/SetColor" and not held:
                held = True
                hass.loop.call_soon_threadsafe(in_flight.set)
                if not release.wait(10):
                    raise AssertionError("The test did not release the first color reply")
            return result

        def observe_arrival():
            nonlocal arrived
            if observed.get():
                return
            observed.set(True)
            arrived += 1
            if arrived == len(calls):
                all_arrived.set()

        async def observe_request(entity, coro):
            observe_arrival()
            return await request_call(entity, coro)

        async def observe_turn_on(entity, **kwargs):
            observe_arrival()
            return await turn_on(entity, **kwargs)

        async def observe_turn_off(entity, **kwargs):
            observe_arrival()
            return await turn_off(entity, **kwargs)

        monkeypatch.setattr(haven_api, "make_request", hold_first_reply)
        monkeypatch.setattr(HavenLight, "async_request_call", observe_request)
        # HA 2024.1 bypasses async_request_call for single-entity services.
        # Current HA uses both hooks; the per-task marker counts each call once.
        monkeypatch.setattr(HavenLight, "async_turn_on", observe_turn_on)
        monkeypatch.setattr(HavenLight, "async_turn_off", observe_turn_off)

        def start(call):
            service, data = call
            tasks.append(asyncio.create_task(hass.services.async_call(
                "light", service, data, blocking=True,
            )))

        try:
            start(calls[0])
            await asyncio.wait_for(in_flight.wait(), 5)
            for call in calls[1:]:
                start(call)
            await asyncio.wait_for(all_arrived.wait(), 5)
            assert len(haven_api.command_calls) == before + 1
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        await async_drain_scheduled_work(hass)
        return outcomes

    return run


@pytest.mark.parametrize(
    ("updates", "expected_ids", "expected_kelvin"),
    [
        ([{"color_temp_kelvin": value} for value in (3500, 3700, 4100, 5000)],
         [3, 4, 6, 8], 5000),
        ([{"rgb_color": (0, 0, 255)}, {"rgb_color": (0, 255, 0)},
          {"color_temp_kelvin": 2700}], [25, 21, 1], 2700),
    ],
    ids=["white-drag", "rgb-to-warm-white"],
)
async def test_color_requests_execute_in_order_and_settle_on_final_intent(
    hass, real_loaded_entry, haven_api, budget_transport, queued_services,
    updates, expected_ids, expected_kelvin,
):
    entity_id = entity_for(hass)
    # Empty successful replies are the observed SetColor response contract.
    budget_transport.command_response_body = b""
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, **update}) for update in updates
    ])
    assert outcomes == [None] * len(updates)
    expected = [command for color_id in expected_ids for command in _color_commands(color_id)]
    assert _commands(haven_api) == expected
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == expected_kelvin

    # A reported state update reconciles values; it must not issue another POST.
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert _commands(haven_api) == expected
    assert hass.states.get(entity_id).attributes["color_temp_kelvin"] == expected_kelvin

    # A later app-side color change must replace the selected white state.
    haven_api.zones[0]["colorId"] = 25
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    state = hass.states.get(entity_id)
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] == (0, 0, 255)
    assert state.attributes["color_temp_kelvin"] is None
    assert _commands(haven_api) == expected


@pytest.mark.parametrize("barrier", ["off", "brightness", "effect", "other-target"])
async def test_mixed_light_requests_preserve_order(
    hass, real_loaded_entry, haven_api, queued_services, barrier
):
    entity_id = entity_for(hass)
    if barrier == "off":
        middle = ("turn_off", {"entity_id": entity_id})
        middle_commands = [("/Commands/Off", ("Zone", 101), None)]
    elif barrier == "brightness":
        middle = ("turn_on", {"entity_id": entity_id, "brightness": 204})
        middle_commands = [("/Commands/Brightness", ("Zone", 101), 8)]
    elif barrier == "effect":
        middle = ("turn_on", {"entity_id": entity_id, "effect": "Fire"})
        middle_commands = _color_commands(12)
    else:
        # A group with the same numeric ID is still a different command target.
        haven_api.groups = [haven_api.group(101, "Other target")]
        coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
        await coordinator.async_refresh()
        await async_drain_scheduled_work(hass)
        middle = ("turn_on", {
            "entity_id": entity_for(hass, kind="group"), "color_temp_kelvin": 3000,
        })
        middle_commands = _color_commands(2, ("Group", 101))
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3500}),
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3700}),
        middle,
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 5000}),
    ])
    assert outcomes == [None] * 4
    assert _commands(haven_api) == (
        _color_commands(3) + _color_commands(4) + middle_commands + _color_commands(8)
    )
    assert hass.states.get(entity_id).attributes["color_temp_kelvin"] == 5000


async def test_failed_color_request_does_not_replay_or_undo_prior_commands(
    hass, real_loaded_entry, haven_api, budget_transport, queued_services, monkeypatch
):
    entity_id = entity_for(hass)
    request = requests.sessions.Session.request

    def reject_final_color(session, method, url, **kwargs):
        final_color = (
            urlsplit(url).path.endswith("/Commands/SetColor")
            and kwargs.get("json", {}).get("colorId") == 8
        )
        if not final_color:
            return request(session, method, url, **kwargs)
        budget_transport.command_status = 400
        try:
            return request(session, method, url, **kwargs)
        finally:
            budget_transport.command_status = None

    monkeypatch.setattr(requests.sessions.Session, "request", reject_final_color)
    before = len(budget_transport.calls)
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": value})
        for value in (3500, 3700, 4100, 5000)
    ])
    assert outcomes[:3] == [None, None, None]
    assert isinstance(outcomes[3], HomeAssistantError)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
        ("POST", "/Commands/SetColor"),
    ]
    assert hass.states.get(entity_id).attributes["color_temp_kelvin"] == 4100


@pytest.mark.skipif(
    HA_VERSION.startswith("2024.1."),
    reason="HA 2024.1 bypasses the platform semaphore for single-entity services",
)
async def test_toggle_waits_for_preceding_turn_on_before_resolving_power(
    hass, real_loaded_entry, haven_api, queued_services
):
    entity_id = entity_for(hass)
    assert hass.states.get(entity_id).state == "off"
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3500}),
        ("toggle", {"entity_id": entity_id}),
    ])
    assert outcomes == [None, None]
    assert _commands(haven_api) == (
        _color_commands(3) + [("/Commands/Off", ("Zone", 101), None)]
    )
    assert hass.states.get(entity_id).state == "off"


@pytest.mark.skipif(
    HA_VERSION.startswith("2024.1."),
    reason="HA 2024.1 bypasses the platform semaphore for single-entity services",
)
async def test_brightness_steps_resolve_after_preceding_commands(
    hass, real_loaded_entry, haven_api, queued_services
):
    haven_api.zones[0].update(isOn=True, lightBrightnessId=5)
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    entity_id = entity_for(hass)
    assert hass.states.get(entity_id).attributes["brightness"] == 128
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3500}),
        ("turn_on", {"entity_id": entity_id, "brightness_step": 26}),
        ("turn_on", {"entity_id": entity_id, "brightness_step": 26}),
    ])
    assert outcomes == [None, None, None]
    assert _commands(haven_api) == _color_commands(3) + [
        ("/Commands/Brightness", ("Zone", 101), 6),
        ("/Commands/Brightness", ("Zone", 101), 7),
    ]
    state = hass.states.get(entity_id)
    assert state.attributes["brightness"] == 178
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 3500


async def test_queued_effect_off_stops_the_in_flight_effect(
    hass, real_loaded_entry, haven_api, queued_services
):
    entity_id = entity_for(hass)
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "effect": "Fire"}),
        ("turn_on", {"entity_id": entity_id, "effect": "off"}),
    ])
    assert outcomes == [None, None]
    assert _commands(haven_api) == _color_commands(12) + _color_commands(1)
    state = hass.states.get(entity_id)
    assert state.attributes["effect"] == "off"
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 2700


async def test_queued_effect_off_preserves_the_preceding_static_color(
    hass, real_loaded_entry, haven_api, queued_services
):
    # At receipt time the cached state is an effect. The commands ahead of
    # effect-off will replace it with static colors before effect-off executes.
    haven_api.zones[0].update(isOn=True, colorId=12)
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    entity_id = entity_for(hass)
    outcomes = await queued_services([
        ("turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3500}),
        ("turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)}),
        ("turn_on", {"entity_id": entity_id, "effect": "off"}),
    ])
    assert outcomes == [None, None, None]
    assert _commands(haven_api) == (
        _color_commands(3) + _color_commands(25) + [("/Commands/On", ("Zone", 101), None)]
    )
    state = hass.states.get(entity_id)
    assert state.attributes["effect"] == "off"
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] == (0, 0, 255)


@pytest.mark.parametrize(
    ("requested", "color_id", "reported"),
    [(2700, 1, 2700), (3000, 2, 3000), (3500, 3, 3500), (3700, 4, 3700),
     (4000, 5, 4000), (4100, 6, 4100), (4700, 7, 4700), (5000, 8, 5000),
     (3200, 2, 3000), (4050, 5, 4000)],
)
async def test_white_preset_roundtrip_and_brightness_preserve_temperature_mode(
    hass, real_loaded_entry, haven_api, requested, color_id, reported
):
    entity_id = entity_for(hass)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "color_temp_kelvin": requested},
        blocking=True,
    )
    assert _commands(haven_api) == _color_commands(color_id)
    state = hass.states.get(entity_id)
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == reported

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "brightness": 204}, blocking=True
    )
    expected = _color_commands(color_id) + [("/Commands/Brightness", ("Zone", 101), 8)]
    assert _commands(haven_api) == expected
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    state = hass.states.get(entity_id)
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == reported
    assert state.attributes["brightness"] == 204
    assert _commands(haven_api) == expected
