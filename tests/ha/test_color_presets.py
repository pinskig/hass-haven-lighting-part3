"""Named static colors use exact Haven IDs without exposing a false effect menu."""

from __future__ import annotations

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from conftest import ACCOUNT_ID, LOCATION_ID, async_drain_scheduled_work
from test_entities import entity_for

COLOR_NAMES = [
    "Red", "Fire", "Pumpkin", "Amber", "Tangerine", "Merigold", "Sunset",
    "Yellow", "Lime", "Light Green", "Green", "Sea Foam", "Turquoise", "Ocean",
    "Deep Blue", "Violet", "Purple", "Lavender", "Pink", "Hot Pink",
]


def preset_for(hass, *, kind="zone", preset="color_preset"):
    unique = f"haven:{ACCOUNT_ID}:{LOCATION_ID}:{kind}:101:{preset}"
    result = er.async_get(hass).async_get_entity_id("select", "haven", unique)
    assert result is not None, unique
    return result


async def select_color(hass, name, *, kind="zone", context=None):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": preset_for(hass, kind=kind), "option": name},
        blocking=True, context=context,
    )


async def test_named_palette_and_group_metadata_share_existing_devices(
    hass, loaded_entry, haven_api
):
    from custom_components.haven.havenlighting.colors import COLOR_BY_ID, HAVEN_COLOR_MAP

    assert HAVEN_COLOR_MAP == dict(zip(COLOR_NAMES, range(11, 31)))
    assert COLOR_BY_ID == dict(zip(range(11, 31), COLOR_NAMES))
    registry = er.async_get(hass)
    light = registry.async_get(entity_for(hass))
    white = registry.async_get(preset_for(hass, preset="white_temperature"))
    color = registry.async_get(preset_for(hass))
    assert color.device_id == white.device_id == light.device_id
    assert color.unique_id == f"{light.unique_id}:color_preset"
    assert white.unique_id == f"{light.unique_id}:white_temperature"
    state = hass.states.get(color.entity_id)
    assert state.name == "Front yard Color preset"
    assert state.attributes["options"] == COLOR_NAMES
    assert state.state == "unknown"

    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.groups = [haven_api.group(101, "Same-number group")]
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    group_color = registry.async_get(preset_for(hass, kind="group"))
    assert group_color.device_id == registry.async_get(entity_for(hass, kind="group")).device_id
    assert group_color.device_id != color.device_id
    await select_color(hass, "Fire", kind="group")
    assert [kwargs["json"] for _method, _path, kwargs in haven_api.command_calls] == [
        {"id": 101, "type": "Group", "colorId": 12}, {"id": 101, "type": "Group"},
    ]
    assert haven_api.zones[0]["colorId"] == 1
    assert hass.states.get(group_color.entity_id).state == "Fire"


@pytest.mark.parametrize("route", ["selector", "legacy_alias"])
@pytest.mark.parametrize(
    ("name", "color_id", "rgb"),
    [("Fire", 12, (255, 21, 0)), ("Sunset", 17, (255, 120, 0)), ("Ocean", 24, None)],
)
async def test_exact_named_colors_and_legacy_aliases_publish_static_state(
    hass, loaded_entry, haven_api, route, name, color_id, rgb
):
    light = entity_for(hass)
    polls_before = len(haven_api.poll_calls)
    if route == "selector":
        await select_color(hass, name)
    else:
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": light, "effect": name}, blocking=True
        )
    assert [(path, kwargs["json"]) for _method, path, kwargs in haven_api.command_calls] == [
        ("/Commands/SetColor", {"id": 101, "type": "Zone", "colorId": color_id}),
        ("/Commands/On", {"id": 101, "type": "Zone"}),
    ]
    state = hass.states.get(light)
    assert state.state == "on"
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] == rgb
    assert state.attributes["brightness"] == 128
    assert state.attributes["effect"] is None
    assert state.attributes["effect_list"] is None
    assert hass.states.get(preset_for(hass)).state == name
    assert hass.states.get(preset_for(hass, preset="white_temperature")).state == "unknown"
    assert len(haven_api.poll_calls) == polls_before


async def test_color_selector_resolves_renamed_light_and_preserves_context(
    hass, loaded_entry, haven_api, monkeypatch
):
    from custom_components.haven.light import HavenLight

    registry = er.async_get(hass)
    registry.async_update_entity(entity_for(hass), new_entity_id="light.renamed_color_target")
    await async_drain_scheduled_work(hass)
    context = Context(parent_id=Context().id)
    received = []
    turn_on = HavenLight.async_turn_on

    async def observe(entity, **kwargs):
        received.append((entity._context, dict(kwargs)))
        await turn_on(entity, **kwargs)

    monkeypatch.setattr(HavenLight, "async_turn_on", observe)
    await select_color(hass, "Sunset", context=context)
    assert len(received) == 1
    assert received[0][0] is context
    assert received[0][1] == {"effect": "Sunset"}
    assert haven_api.zones[0]["colorId"] == 17
    assert hass.states.get("light.renamed_color_target").attributes["rgb_color"] == (255, 120, 0)


async def test_color_and_white_selectors_follow_shared_snapshot_without_commands(
    hass, loaded_entry, haven_api
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    color = preset_for(hass)
    white = preset_for(hass, preset="white_temperature")
    await select_color(hass, "Fire")
    assert hass.states.get(color).state == "Fire"
    assert hass.states.get(white).state == "unknown"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": white, "option": "2700 K"}, blocking=True
    )
    assert hass.states.get(white).state == "2700 K"
    assert hass.states.get(color).state == "unknown"
    commands_before = list(haven_api.command_calls)

    haven_api.zones[0]["colorId"] = 17
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert hass.states.get(color).state == "Sunset"
    assert hass.states.get(white).state == "unknown"
    # Native patterns are not selectable static colors; unknown data is not
    # replaced with a remembered choice from either selector.
    for color_id in (33, 34, 35, 999, None):
        haven_api.zones[0]["colorId"] = color_id
        await coordinator.async_refresh()
        await async_drain_scheduled_work(hass)
        assert hass.states.get(color).state == "unknown"
        assert hass.states.get(white).state == "unknown"
    assert haven_api.command_calls == commands_before


async def test_rejected_color_selection_does_not_publish_a_false_choice(
    hass, loaded_entry, haven_api
):
    from custom_components.haven.havenlighting.exceptions import ApiError

    haven_api.error = ApiError("Synthetic Haven command rejection", code=400)
    with pytest.raises(HomeAssistantError):
        await select_color(hass, "Fire")
    assert [path for _method, path, _kwargs in haven_api.command_calls] == ["/Commands/SetColor"]
    assert haven_api.zones[0]["colorId"] == 1
    assert hass.states.get(preset_for(hass)).state == "unknown"
    assert hass.states.get(preset_for(hass, preset="white_temperature")).state == "2700 K"
    assert hass.states.get(entity_for(hass)).state == "off"


async def test_pattern_name_is_not_a_color_selector_option(hass, loaded_entry, haven_api):
    with pytest.raises(HomeAssistantError):
        await select_color(hass, "Warm Colors")
    assert haven_api.command_calls == []
    assert hass.states.get(preset_for(hass)).state == "unknown"


@pytest.mark.parametrize(
    ("initial_color", "initial_on"), [(1, True), (24, False)], ids=["white", "ocean_off"]
)
async def test_explicit_ocean_scene_restores_color_power_and_brightness(
    hass, loaded_entry, haven_api, initial_color, initial_on
):
    coordinator = hass.data["haven"][loaded_entry.entry_id]["coordinators"][0]
    haven_api.zones[0].update(
        colorId=initial_color, isOn=initial_on, lightBrightnessId=3
    )
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    light = entity_for(hass)
    color = preset_for(hass)

    # This is an explicit YAML scene, not a snapshot of the light's unknown
    # Ocean RGB swatch. Power/brightness must also be present: HA skips a
    # selector whose option is already Ocean, even when its light is off.
    assert await async_setup_component(hass, "scene", {"scene": [{
        "name": "Haven Ocean",
        "entities": {
            color: "Ocean",
            light: {"state": "on", "brightness": 204},
        },
    }]})
    await async_drain_scheduled_work(hass)
    await hass.services.async_call(
        "scene", "turn_on", {"entity_id": "scene.haven_ocean"}, blocking=True
    )
    await async_drain_scheduled_work(hass)

    state = hass.states.get(light)
    assert state.state == "on"
    assert state.attributes["brightness"] == 204
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] is None
    assert state.attributes["effect"] is None
    assert hass.states.get(color).state == "Ocean"
    assert haven_api.zones[0]["colorId"] == 24
    assert haven_api.zones[0]["isOn"] is True
    assert haven_api.zones[0]["lightBrightnessId"] == 8
    color_commands = [
        kwargs["json"]["colorId"] for _method, path, kwargs in haven_api.command_calls
        if path == "/Commands/SetColor"
    ]
    assert color_commands == ([24] if initial_color == 1 else [])
    if initial_color == 24:
        assert [path for _method, path, _kwargs in haven_api.command_calls] == [
            "/Commands/Brightness"
        ]
