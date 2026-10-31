"""Contract tests for the HA adapter using deliberately narrow framework stubs.

These exercise the integration's own methods and calls across the HA boundary.
They do not install Home Assistant, execute its scheduler or verify its actual
Debouncer implementation. HTTP remains fully mocked throughout.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from enum import Enum, IntFlag
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests


class HomeAssistantError(Exception):
    pass


class ConfigEntryAuthFailed(HomeAssistantError):
    pass


class ConfigEntryNotReady(HomeAssistantError):
    pass


class UpdateFailed(HomeAssistantError):
    pass


class ConfigEntry:
    def __init__(self):
        self.entry_id = "test-entry"
        self.data = {"email": "test@example.invalid", "password": "test-password"}


class ConfigFlow:
    """Expose form/entry call points without implementing HA flow validation."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def __init__(self):
        self.context = {}
        self._async_current_entries = Mock(return_value=[])
        self.async_set_unique_id = AsyncMock()
        self._abort_if_unique_id_configured = Mock()
        self.async_abort = Mock(side_effect=lambda **kwargs: {"type": "abort", **kwargs})
        self.async_create_entry = Mock(side_effect=lambda **kwargs: {"type": "create_entry", **kwargs})
        self.async_show_form = Mock(side_effect=lambda **kwargs: {"type": "form", **kwargs})


class FakeHomeAssistant:
    def __init__(self):
        self.data = {}
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_get_entry=Mock(), async_update_entry=Mock(), async_reload=AsyncMock(),
        )

        async def executor(function, *args):
            return function(*args)

        self.async_add_executor_job = AsyncMock(side_effect=executor)


class Debouncer:
    def __init__(self, hass, logger, *, cooldown, immediate):
        self.cooldown = cooldown
        self.immediate = immediate


class DataUpdateCoordinator:
    """Capture constructor arguments and expose the adapter's HA call points."""

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name, update_interval, request_refresh_debouncer):
        self.hass = hass
        self.name = name
        self.update_interval = update_interval
        self.request_refresh_debouncer = request_refresh_debouncer
        self.data = {}
        self.last_update_success = True
        self.async_request_refresh = AsyncMock()
        self.async_shutdown = AsyncMock()

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()


class CoordinatorEntity:
    should_poll = False

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.async_write_ha_state = Mock()

    @property
    def available(self):
        return self.coordinator.last_update_success

    def _handle_coordinator_update(self):
        self.async_write_ha_state()


class LightEntity:
    pass


class ColorMode(str, Enum):
    RGB = "rgb"
    COLOR_TEMP = "color_temp"


class LightEntityFeature(IntFlag):
    EFFECT = 1


def framework_modules():
    """Build only the imported HA symbols; none implements request scheduling."""
    names = [
        "homeassistant", "homeassistant.config_entries", "homeassistant.const",
        "homeassistant.core", "homeassistant.exceptions", "homeassistant.components",
        "homeassistant.components.light", "homeassistant.helpers",
        "homeassistant.helpers.debounce", "homeassistant.helpers.update_coordinator",
        "homeassistant.helpers.entity_registry", "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_platform", "homeassistant.helpers.entity",
        "homeassistant.data_entry_flow", "voluptuous",
    ]
    modules = {name: ModuleType(name) for name in names}
    for name, module in modules.items():
        module.__path__ = []
        if "." in name:
            parent, attribute = name.rsplit(".", 1)
            setattr(modules[parent], attribute, module)
    modules["homeassistant.config_entries"].ConfigEntry = ConfigEntry
    modules["homeassistant.config_entries"].ConfigFlow = ConfigFlow
    modules["homeassistant.const"].Platform = SimpleNamespace(LIGHT="light")
    modules["homeassistant.const"].CONF_EMAIL = "email"
    modules["homeassistant.const"].CONF_PASSWORD = "password"
    modules["homeassistant.data_entry_flow"].FlowResult = dict
    # These schema placeholders allow form-construction calls, not validation.
    modules["voluptuous"].Schema = lambda fields: fields
    modules["voluptuous"].Required = lambda field: field
    modules["homeassistant.core"].HomeAssistant = FakeHomeAssistant
    modules["homeassistant.core"].callback = lambda method: method
    for name, cls in {
        "HomeAssistantError": HomeAssistantError,
        "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
        "ConfigEntryNotReady": ConfigEntryNotReady,
    }.items():
        setattr(modules["homeassistant.exceptions"], name, cls)
    modules["homeassistant.helpers.debounce"].Debouncer = Debouncer
    for name, cls in {
        "DataUpdateCoordinator": DataUpdateCoordinator,
        "CoordinatorEntity": CoordinatorEntity,
        "UpdateFailed": UpdateFailed,
    }.items():
        setattr(modules["homeassistant.helpers.update_coordinator"], name, cls)
    light_module = modules["homeassistant.components.light"]
    for name, value in {
        "ATTR_BRIGHTNESS": "brightness", "ATTR_RGB_COLOR": "rgb_color",
        "ATTR_COLOR_TEMP_KELVIN": "color_temp_kelvin", "ATTR_EFFECT": "effect",
        "ColorMode": ColorMode, "LightEntity": LightEntity,
        "LightEntityFeature": LightEntityFeature,
    }.items():
        setattr(light_module, name, value)
    modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    modules["homeassistant.helpers.entity"].DeviceInfo = dict
    return modules


class ObservedAsyncLock:
    def __init__(self, participants):
        self._lock = asyncio.Lock()
        self._participants = participants
        self._attempts = 0
        self.all_attempting = asyncio.Event()

    async def __aenter__(self):
        self._attempts += 1
        if self._attempts == self._participants:
            self.all_attempting.set()
        await self._lock.acquire()

    async def __aexit__(self, *args):
        self._lock.release()


class HomeAssistantBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._path = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, cls._path)
        cls._previous = {
            name: module for name, module in sys.modules.items()
            if name == "custom_components.haven" or name.startswith("custom_components.haven.")
        }
        for name in cls._previous:
            del sys.modules[name]
        cls._framework = patch.dict(sys.modules, framework_modules())
        cls._framework.start()
        cls.integration = importlib.import_module("custom_components.haven")
        cls.coordinator_module = importlib.import_module("custom_components.haven.coordinator")
        cls.light_module = importlib.import_module("custom_components.haven.light")
        cls.flow_module = importlib.import_module("custom_components.haven.config_flow")
        cls.errors = importlib.import_module("custom_components.haven.havenlighting.exceptions")
        cls.credentials_module = importlib.import_module("custom_components.haven.havenlighting.credentials")
        cls.client_module = importlib.import_module("custom_components.haven.havenlighting.client")

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == "custom_components.haven" or name.startswith("custom_components.haven."):
                del sys.modules[name]
        cls._framework.stop()
        sys.modules.update(cls._previous)
        sys.path.remove(cls._path)

    def setUp(self):
        network = patch.object(requests, "request", side_effect=AssertionError("Unexpected HTTP request"))
        self.request = network.start()
        self.addCleanup(network.stop)
        self.hass = FakeHomeAssistant()
        self.entry = ConfigEntry()
        self.location = SimpleNamespace(location_id=12345, lights={}, refresh_devices=Mock())
        self.coordinator = self.coordinator_module.HavenCoordinator(self.hass, self.entry, self.location)

    def make_entity(self):
        light = SimpleNamespace(
            id=101, name="Test zone", _type="Zone", is_on=False, brightness=128,
            turn_on=Mock(), turn_off=Mock(), set_brightness=Mock(), set_color=Mock(),
        )
        self.location.lights = {101: light}
        self.coordinator.data = self.location.lights
        return self.light_module.HavenLight(light, self.coordinator), light

    async def test_entities_use_coordinator_without_individual_polling(self):
        entity, _ = self.make_entity()
        self.assertIsInstance(entity, CoordinatorEntity)
        self.assertFalse(entity.should_poll)
        self.assertTrue(entity.available)
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)

    async def test_successful_commands_request_shared_debounced_refresh(self):
        entity, light = self.make_entity()
        await entity.async_turn_on()
        await entity.async_turn_off()
        light.turn_on.assert_called_once_with()
        light.turn_off.assert_called_once_with()
        self.assertEqual(self.coordinator.async_request_refresh.await_count, 2)
        self.location.refresh_devices.assert_not_called()
        self.assertEqual(entity.async_write_ha_state.call_count, 2)
        debouncer = self.coordinator.request_refresh_debouncer
        self.assertFalse(debouncer.immediate)
        self.assertGreaterEqual(debouncer.cooldown, 5)

    async def test_rejected_command_becomes_homeassistant_error(self):
        entity, light = self.make_entity()
        rejection = self.errors.ApiError("Rate limited", code=429, retry_after=3600)
        light.turn_on.side_effect = rejection
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_turn_on()
        self.assertIs(raised.exception.__cause__, rejection)
        self.coordinator.async_request_refresh.assert_not_awaited()
        self.location.refresh_devices.assert_not_called()
        entity.async_write_ha_state.assert_not_called()

    async def test_coordinator_429_preserves_server_wait(self):
        self.location.refresh_devices.side_effect = self.errors.ApiError(
            "Rate limited", code=429, retry_after=3600
        )
        with self.assertRaises(UpdateFailed) as raised:
            await self.coordinator._async_update_data()
        self.assertGreaterEqual(raised.exception.retry_after, 3600)
        self.assertGreaterEqual(self.coordinator.update_interval.total_seconds(), 3600)

    async def test_coordinator_authentication_error_requests_reauthentication(self):
        self.location.refresh_devices.side_effect = self.errors.AuthenticationError("Expired")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._async_update_data()

    async def test_successful_recovery_restores_minute_cadence(self):
        self.location.refresh_devices.side_effect = self.errors.ApiError("Limited", code=429, retry_after=3600)
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()
        self.location.refresh_devices.side_effect = None
        self.location.lights = {101: object()}
        self.assertIs(await self.coordinator._async_update_data(), self.location.lights)
        self.assertEqual(self.coordinator.update_interval, timedelta(seconds=60))

    async def test_concurrent_coordinator_refreshes_are_coalesced(self):
        observed = ObservedAsyncLock(8)
        self.coordinator._refresh_lock = observed
        release = asyncio.Event()

        async def executor(function, *args):
            await release.wait()
            return function(*args)

        self.hass.async_add_executor_job.side_effect = executor
        tasks = [asyncio.create_task(self.coordinator._async_update_data()) for _ in range(8)]
        try:
            await asyncio.wait_for(observed.all_attempting.wait(), timeout=3)
        finally:
            release.set()
        await asyncio.gather(*tasks)
        self.location.refresh_devices.assert_called_once_with(True)

    async def test_setup_retries_keep_the_client_and_its_cooldown(self):
        clock = SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 1_800_000_000.0,
                                sleep=lambda _: None)
        result = requests.Response()
        result.status_code = 429
        result.headers["Retry-After"] = "3600"
        result._content = json.dumps({}).encode("utf-8")
        self.request.side_effect = None
        self.request.return_value = result
        with patch.object(self.credentials_module, "time", clock), patch.object(self.client_module, "setup_logging"):
            client = self.integration.HavenClient()
            with patch.object(self.integration, "HavenClient", return_value=client) as factory:
                for _ in range(2):
                    with self.assertRaises(ConfigEntryNotReady) as raised:
                        await self.integration.async_setup_entry(self.hass, self.entry)
                    self.assertEqual(raised.exception.retry_after, 3600)
                factory.assert_called_once_with()
                self.assertIs(self.hass.data["haven"][self.entry.entry_id]["client"], client)
        self.assertEqual(self.request.call_count, 1)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_transient_setup_error_is_retryable_not_bad_credentials(self):
        client = SimpleNamespace(is_authenticated=False, authenticate=Mock(
            side_effect=self.errors.ApiError("Temporary service failure")
        ))
        with patch.object(self.integration, "HavenClient", return_value=client):
            with self.assertRaises(ConfigEntryNotReady):
                await self.integration.async_setup_entry(self.hass, self.entry)

    async def test_setup_auth_failures_reauthenticate_the_existing_client(self):
        for phase in ("discovery", "coordinator"):
            with self.subTest(phase=phase):
                hass = FakeHomeAssistant()
                first_discovery = (
                    self.errors.AuthenticationError("Expired")
                    if phase == "discovery" else {12345: self.location}
                )
                client = SimpleNamespace(
                    is_authenticated=True,
                    authenticate=Mock(return_value=True),
                    discover_locations=Mock(side_effect=[first_discovery, {}]),
                )
                coordinator = SimpleNamespace(
                    async_config_entry_first_refresh=AsyncMock(side_effect=ConfigEntryAuthFailed("Expired")),
                    async_shutdown=AsyncMock(),
                )
                with patch.object(self.integration, "HavenClient", return_value=client) as factory, \
                        patch.object(self.integration, "HavenCoordinator", return_value=coordinator):
                    with self.assertRaises(ConfigEntryAuthFailed):
                        await self.integration.async_setup_entry(hass, self.entry)
                    runtime = hass.data["haven"][self.entry.entry_id]
                    self.assertIs(runtime["client"], client)
                    self.assertTrue(runtime["authentication_required"])
                    self.assertTrue(await self.integration.async_setup_entry(hass, self.entry))
                    factory.assert_called_once_with()
                client.authenticate.assert_called_once_with("test@example.invalid", "test-password")
                self.assertFalse(runtime["authentication_required"])
                if phase == "coordinator":
                    coordinator.async_shutdown.assert_awaited_once_with()

    async def test_cancelled_platform_setup_shuts_down_new_coordinators(self):
        client = SimpleNamespace(
            is_authenticated=True,
            discover_locations=Mock(return_value={12345: self.location}),
        )
        coordinator = SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(), async_shutdown=AsyncMock()
        )
        self.hass.config_entries.async_forward_entry_setups.side_effect = asyncio.CancelledError()
        with patch.object(self.integration, "HavenClient", return_value=client), \
                patch.object(self.integration, "HavenCoordinator", return_value=coordinator):
            with self.assertRaises(asyncio.CancelledError):
                await self.integration.async_setup_entry(self.hass, self.entry)
        coordinator.async_shutdown.assert_awaited_once_with()
        self.assertEqual(self.hass.data["haven"][self.entry.entry_id]["coordinators"], [])
        self.assertIs(self.hass.data["haven"][self.entry.entry_id]["client"], client)

    async def test_duplicate_legacy_email_is_rejected_before_authentication(self):
        client = SimpleNamespace(authenticate=Mock())
        with patch.object(self.flow_module, "HavenClient", return_value=client):
            flow = self.flow_module.HavenConfigFlow()
        flow.hass = self.hass
        flow._async_current_entries.return_value = [SimpleNamespace(
            unique_id=None, data={"email": "  TeSt@Example.INVALID  "}
        )]
        result = await flow.async_step_user(
            {"email": " test@example.invalid ", "password": "test-password"}
        )
        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})
        client.authenticate.assert_not_called()
        self.hass.async_add_executor_job.assert_not_awaited()
        self.request.assert_not_called()

    async def test_successful_reauthentication_updates_existing_entry_and_reloads(self):
        client = SimpleNamespace(authenticate=Mock(return_value=True))
        with patch.object(self.flow_module, "HavenClient", return_value=client):
            flow = self.flow_module.HavenConfigFlow()
        flow.hass = self.hass
        flow.context = {"entry_id": self.entry.entry_id}
        self.hass.config_entries.async_get_entry.return_value = self.entry
        await flow.async_step_reauth(self.entry.data)
        result = await flow.async_step_reauth_confirm({"password": "replacement-password"})
        self.assertEqual(result, {"type": "abort", "reason": "reauth_successful"})
        self.hass.config_entries.async_update_entry.assert_called_once_with(
            self.entry, data={"email": "test@example.invalid", "password": "replacement-password"}
        )
        self.hass.config_entries.async_reload.assert_awaited_once_with(self.entry.entry_id)
        client.authenticate.assert_called_once_with("test@example.invalid", "replacement-password")
        flow.async_create_entry.assert_not_called()

    async def test_repeated_setup_form_preserves_client_cooldown(self):
        clock = SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 1_800_000_000.0,
                                sleep=lambda _: None)
        result = requests.Response()
        result.status_code = 429
        result.headers["Retry-After"] = "3600"
        result._content = json.dumps({}).encode("utf-8")
        self.request.side_effect = None
        self.request.return_value = result
        with patch.object(self.credentials_module, "time", clock), patch.object(self.client_module, "setup_logging"):
            client = self.integration.HavenClient()
            with patch.object(self.flow_module, "HavenClient", return_value=client) as factory:
                flow = self.flow_module.HavenConfigFlow()
                flow.hass = self.hass
                for _ in range(2):
                    form = await flow.async_step_user(
                        {"email": "test@example.invalid", "password": "test-password"}
                    )
                    self.assertEqual(form["type"], "form")
                    self.assertEqual(form["errors"], {"base": "cannot_connect"})
                factory.assert_called_once_with()
                self.assertIs(flow._client, client)
        self.assertEqual(self.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
