"""Persisted account budgets exercised through real clients and HA storage.

Only the final Session.request boundary is replaced. The Haven client, token
handling, RequestGate, config-entry reload, config flow, and HA Store are real.
Unlike the other framework tests, this module writes actual temporary Store
files so restoration is checked against serialized data, not a mocked load.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import time

import pytest
import requests
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError

from conftest import ACCOUNT_ID, LOCATION_ID
from test_entities import entity_for


@pytest.fixture
def hass_storage(monkeypatch, hass_config_dir):
    """Disable the upstream in-memory storage replacement for these tests."""
    # The 2024 fixture creates HA through get_test_config_dir directly; the
    # current fixture accepts hass_config_dir. Isolate both before HA starts.
    monkeypatch.setattr(
        "pytest_homeassistant_custom_component.common.get_test_config_dir",
        lambda *parts: str(Path(hass_config_dir).joinpath(*parts)),
    )
    return {}


@pytest.fixture
def hass_config_dir(tmp_path):
    path = tmp_path / "homeassistant"
    path.mkdir()
    return str(path)


@pytest.fixture
def gate_clock(monkeypatch, custom_integration_and_no_http):
    from custom_components.haven.havenlighting import request_gate

    clock = SimpleNamespace(wall=time.time(), monotonic=50000.0)
    monkeypatch.setattr(
        request_gate,
        "time",
        SimpleNamespace(
            time=lambda: clock.wall,
            monotonic=lambda: clock.monotonic,
            sleep=lambda delay: setattr(clock, "monotonic", clock.monotonic + delay),
        ),
    )
    return clock


@pytest.fixture
def budget_transport(monkeypatch, haven_api, custom_integration_and_no_http):
    """Return real Response objects and observe actual session/pool closure."""
    transport = SimpleNamespace(calls=[], command_status=None, closed_sessions=[], closed_adapters=[])

    def response(status, data, url, *, retry_after=None):
        result = requests.Response()
        result.status_code = status
        result.url = url
        result._content = json.dumps(data).encode("utf-8")
        result._content_consumed = True
        result.encoding = "utf-8"
        if retry_after is not None:
            result.headers["Retry-After"] = str(retry_after)
        return result

    def request(session, method, url, **kwargs):
        path = urlsplit(url).path.removeprefix("/api")
        transport.calls.append((session, method, path))
        if path == "/Auth/Authenticate":
            return response(200, {
                "token": "test-access-token", "refreshToken": "test-refresh-token", "id": int(ACCOUNT_ID)
            }, url)
        if path == "/user/GetUserInfo":
            return response(200, {
                "defaultLocationId": LOCATION_ID, "firstName": "Test", "lastName": "Account"
            }, url)
        if path.startswith("/Commands/") and transport.command_status is not None:
            return response(transport.command_status, {}, url, retry_after=3600)
        return response(200, haven_api.make_request(method, path, **kwargs), url)

    original_close = requests.sessions.Session.close
    original_adapter_close = requests.adapters.HTTPAdapter.close

    def close(session):
        transport.closed_sessions.append(session)
        return original_close(session)

    def close_adapter(adapter):
        transport.closed_adapters.append(adapter)
        return original_adapter_close(adapter)

    # This override runs after the autouse HTTP prohibition. All requests remain
    # in process: unknown endpoints fail rather than falling through to HTTP.
    monkeypatch.setattr(requests.sessions.Session, "request", request)
    monkeypatch.setattr(requests.sessions.Session, "close", close)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "close", close_adapter)
    return transport


@pytest.fixture
def real_clients(monkeypatch, budget_transport):
    from custom_components.haven.havenlighting import HavenClient

    clients = []

    def construct(*args, **kwargs):
        client = HavenClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("custom_components.haven.HavenClient", construct)
    monkeypatch.setattr("custom_components.haven.config_flow.HavenClient", construct)
    return SimpleNamespace(clients=clients, construct=construct)


@pytest.fixture
async def real_loaded_entry(hass, entry, gate_clock, budget_transport, real_clients):
    from custom_components.haven.runtime import async_get_account_store

    accounts = await async_get_account_store(hass)
    gate = await accounts.async_get_gate(entry.data["email"])
    # Keep the real shared gate while removing incidental pacing delays from
    # these persistence tests; cooldown calculation and enforcement are intact.
    gate.min_request_interval = 0
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert real_clients.clients[-1].is_authenticated
    assert {call[2] for call in budget_transport.calls} >= {
        "/Auth/Authenticate", "/user/GetUserInfo",
        f"/LightAndZones/OrderedList/{LOCATION_ID}",
        f"/Group/AllGroupsByLocation/{LOCATION_ID}",
    }
    yield SimpleNamespace(entry=entry, accounts=accounts, gate=gate, client=real_clients.clients[-1])
    if hass.config_entries.async_get_entry(entry.entry_id) is not None:
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


async def throttle_command(hass, context, transport, status):
    transport.command_status = status
    before = len(transport.calls)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": entity_for(hass)}, blocking=True
        )
    assert len(transport.calls) == before + 1
    assert context.gate.rate_limit_remaining == 3600
    await context.accounts.async_flush()


async def read_saved_budget(hass, email):
    from custom_components.haven.runtime import account_key

    path = Path(hass.config.config_dir) / ".storage" / "haven.request_budgets"
    text = await hass.async_add_executor_job(path.read_text)
    document = json.loads(text)
    assert document["version"] == 1
    # Persistence contains the budget, never credentials or even the email.
    assert email not in text
    assert "test-password" not in text
    assert "test-access-token" not in text
    return document["data"]["accounts"][account_key(email)]


@pytest.mark.parametrize("status", [429, 503])
async def test_active_budget_survives_real_entry_reload_without_http(
    hass, real_loaded_entry, budget_transport, real_clients, gate_clock, status
):
    from custom_components.haven.havenlighting.exceptions import ApiError

    context = real_loaded_entry
    await throttle_command(hass, context, budget_transport, status)
    saved = await read_saved_budget(hass, context.entry.data["email"])
    assert saved["status"] == status
    assert saved["retry_at"] == pytest.approx(gate_clock.wall + 3600)
    before = len(budget_transport.calls)

    await hass.config_entries.async_reload(context.entry.entry_id)
    await hass.async_block_till_done()
    assert context.entry.state is config_entries.ConfigEntryState.SETUP_RETRY
    assert len(budget_transport.calls) == before
    replacement = real_clients.clients[-1]
    assert replacement is not context.client
    assert replacement.request_gate is context.gate
    assert context.client.closed
    assert context.client._credentials._session in budget_transport.closed_sessions
    with pytest.raises(ApiError) as blocked:
        await hass.async_add_executor_job(replacement.authenticate, "test@example.invalid", "test-password")
    assert blocked.value.code == status
    assert blocked.value.rate_limited_locally
    assert len(budget_transport.calls) == before
    assert (await read_saved_budget(hass, context.entry.data["email"]))["retry_at"] == saved["retry_at"]


@pytest.mark.parametrize("status", [429, 503])
async def test_new_account_store_restores_wall_clock_budget_from_disk(
    hass, real_loaded_entry, budget_transport, gate_clock, status
):
    from custom_components.haven.havenlighting.credentials import Credentials
    from custom_components.haven.havenlighting.exceptions import ApiError
    from custom_components.haven.runtime import AccountGateStore

    context = real_loaded_entry
    await throttle_command(hass, context, budget_transport, status)
    saved = await read_saved_budget(hass, context.entry.data["email"])
    assert await hass.config_entries.async_unload(context.entry.entry_id)
    await hass.async_block_till_done()

    # Simulate a different process's monotonic origin after two elapsed minutes.
    gate_clock.wall += 120
    gate_clock.monotonic = 10.0
    restarted = AccountGateStore(hass)
    hass.data["haven_accounts"] = restarted
    restored_gate = await restarted.async_get_gate(context.entry.data["email"])
    assert restored_gate is not context.gate
    assert restored_gate.rate_limit_remaining == 3480
    assert restored_gate.snapshot()["retry_at"] == saved["retry_at"]

    credentials = Credentials(request_gate=restored_gate)
    before = len(budget_transport.calls)
    try:
        with pytest.raises(ApiError) as blocked:
            await hass.async_add_executor_job(
                lambda: credentials.make_request(
                    "POST", "/Commands/Off", json={"id": 101, "type": "Zone"}, use_prod_api=True
                )
            )
        assert blocked.value.code == status
        assert blocked.value.retry_after == 3480
        assert blocked.value.rate_limited_locally
        assert len(budget_transport.calls) == before
    finally:
        await hass.async_add_executor_job(credentials.close)
    assert credentials.closed


@pytest.mark.parametrize("status", [429, 503])
async def test_separate_reauth_flow_shares_budget_and_closes_its_real_pool(
    hass, real_loaded_entry, budget_transport, real_clients, status
):
    context = real_loaded_entry
    await throttle_command(hass, context, budget_transport, status)
    before = len(budget_transport.calls)
    form = await hass.config_entries.flow.async_init(
        "haven", context={"source": config_entries.SOURCE_REAUTH, "entry_id": context.entry.entry_id},
        data=context.entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], user_input={"password": "replacement-password"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}
    assert len(budget_transport.calls) == before
    flow_client = real_clients.clients[-1]
    assert flow_client is not context.client
    assert flow_client.request_gate is context.gate
    assert flow_client.closed
    session = flow_client._credentials._session
    assert session in budget_transport.closed_sessions
    assert set(session.adapters.values()) <= set(budget_transport.closed_adapters)
    assert not context.client.closed
    assert context.entry.data["password"] == "test-password"
