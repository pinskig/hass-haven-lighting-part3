"""Real Home Assistant fixtures with only the Haven service boundary replaced.

No Home Assistant modules, registries, coordinators, debouncers, or entity
platforms are stubbed. pytest-socket blocks network sockets; the additional
requests guard makes any accidental Haven HTTP call fail at its entry point.
"""

from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from pytest_homeassistant_custom_component.common import MockConfigEntry

LOCATION_ID = 123456
ACCOUNT_ID = "77"


async def async_drain_scheduled_work(hass):
    """Wait for real timer-triggered background jobs across both HA APIs."""
    if "wait_background_tasks" in inspect.signature(hass.async_block_till_done).parameters:
        await hass.async_block_till_done(wait_background_tasks=True)
    else:
        await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def custom_integration_and_no_http(enable_custom_integrations, monkeypatch):
    # The upstream test plugin ships its own regular custom_components package.
    # Include this checkout in that package's search path without replacing any
    # HA modules or requiring an unrelated package-marker change in production.
    import custom_components

    checkout_components = str(Path(__file__).resolve().parents[2] / "custom_components")
    monkeypatch.setattr(
        custom_components, "__path__", [checkout_components, *custom_components.__path__]
    )

    def forbidden_http(*args, **kwargs):
        raise AssertionError("A real HTTP request escaped the fake Haven boundary")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_http)


@pytest.fixture
def load_registries():
    return True


class FakeHavenAPI:
    """Deterministic external-service responses consumed by the real client model."""

    def __init__(self):
        self.zones = [self.zone(101, "Front yard")]
        self.groups = []
        self.calls = []
        self.error = None

    @staticmethod
    def zone(identifier, name, *, is_on=False, brightness=5, color=1):
        return {
            "id": identifier,
            "name": name,
            "isZone": True,
            "isOn": is_on,
            "lightBrightnessId": brightness,
            "colorId": color,
            "locationName": "Test location",
        }

    @staticmethod
    def group(identifier, name, *, is_on=False, brightness=5, color=1):
        return {
            "groupId": identifier,
            "groupName": name,
            "isOn": is_on,
            "brightnessId": brightness,
            "colorId": color,
        }

    @property
    def poll_calls(self):
        return [call for call in self.calls if call[0] == "GET"]

    @property
    def command_calls(self):
        return [call for call in self.calls if call[0] == "POST"]

    def make_request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if self.error is not None:
            raise self.error
        if method == "GET" and path.startswith("/LightAndZones/OrderedList/"):
            return deepcopy(self.zones)
        if method == "GET" and path.startswith("/Group/AllGroupsByLocation/"):
            return deepcopy(self.groups)
        if method == "POST" and path.startswith("/Commands/"):
            payload = kwargs["json"]
            is_group = payload["type"] == "Group"
            records = self.groups if is_group else self.zones
            id_field = "groupId" if is_group else "id"
            record = next(item for item in records if item[id_field] == payload["id"])
            if path.endswith("/On"):
                record["isOn"] = True
            elif path.endswith("/Off"):
                record["isOn"] = False
            elif path.endswith("/Brightness"):
                record["brightnessId" if is_group else "lightBrightnessId"] = payload["brightness"]
                record["isOn"] = True
            elif path.endswith("/SetColor"):
                record["colorId"] = payload["colorId"]
            else:
                raise AssertionError(f"Unexpected command {path}")
            return {}
        raise AssertionError(f"Unexpected Haven call: {method} {path}")


@pytest.fixture
def haven_api():
    return FakeHavenAPI()


@pytest.fixture
def client_factory(haven_api, monkeypatch):
    from custom_components.haven.havenlighting.devices.location import Location

    class FakeClient:
        def __init__(self, *, request_gate=None):
            self.request_gate = request_gate
            self.account_id = ACCOUNT_ID
            self.is_authenticated = True
            self.authenticate = Mock(return_value=True)
            self.close = Mock()
            self.location = Location(
                haven_api, LOCATION_ID, {"name": "Test location", "ownerName": "Test"}
            )
            self.discover_locations = Mock(return_value={LOCATION_ID: self.location})

    class Factory:
        def __init__(self):
            self.clients = []

        def __call__(self, *args, **kwargs):
            client = FakeClient(**kwargs)
            self.clients.append(client)
            return client

    factory = Factory()
    monkeypatch.setattr("custom_components.haven.HavenClient", factory)
    monkeypatch.setattr("custom_components.haven.config_flow.HavenClient", factory)
    return factory


@pytest.fixture
def entry(hass):
    result = MockConfigEntry(
        domain="haven",
        title="Test Haven",
        unique_id="test@example.invalid",
        data={"email": "test@example.invalid", "password": "test-password"},
    )
    result.add_to_hass(hass)
    return result


@pytest.fixture
async def loaded_entry(hass, entry, client_factory):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    # Both supported HA fixture versions run their real unload hooks here.
    if entry.entry_id in hass.data.get("haven", {}):
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
