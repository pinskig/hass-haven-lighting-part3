"""Persisted account budgets exercised through real clients and HA storage.

Only the final Session.request boundary is replaced. The Haven client, token
handling, RequestGate, config-entry reload, config flow, and HA Store are real.
Unlike the other framework tests, this module writes actual temporary Store
files so restoration is checked against serialized data, not a mocked load.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import time

import pytest
import requests
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import ACCOUNT_ID, LOCATION_ID, async_drain_scheduled_work
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
    transport = SimpleNamespace(
        calls=[], login_calls=[], login_status=200, login_retry_after=None,
        command_status=None, command_success_status=200, command_response_body=None,
        closed_sessions=[], closed_adapters=[],
    )

    def response(status, data, url, *, retry_after=None, raw_body=None):
        result = requests.Response()
        result.status_code = status
        result.url = url
        result._content = json.dumps(data).encode("utf-8") if raw_body is None else raw_body
        result._content_consumed = True
        result.encoding = "utf-8"
        if retry_after is not None:
            result.headers["Retry-After"] = str(retry_after)
        return result

    def request(session, method, url, **kwargs):
        path = urlsplit(url).path.removeprefix("/api")
        transport.calls.append((session, method, path))
        if path == "/Auth/Authenticate":
            transport.login_calls.append((session, dict(kwargs.get("json", {}))))
            if transport.login_status != 200:
                return response(
                    transport.login_status, {"message": "Synthetic sign-in rejection"},
                    url, retry_after=transport.login_retry_after,
                )
            return response(200, {
                "token": "test-access-token", "refreshToken": "test-refresh-token", "id": int(ACCOUNT_ID)
            }, url)
        if path == "/user/GetUserInfo":
            return response(200, {
                "defaultLocationId": LOCATION_ID, "firstName": "Test", "lastName": "Account"
            }, url)
        if path.startswith("/Commands/") and transport.command_status is not None:
            return response(transport.command_status, {}, url, retry_after=3600)
        data = haven_api.make_request(method, path, **kwargs)
        if path.startswith("/Commands/"):
            # The provider may apply a command before its reply is decoded.
            return response(
                transport.command_success_status, data, url,
                raw_body=transport.command_response_body,
            )
        return response(200, data, url)

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


async def test_login_400_stops_setup_retries_and_recovers_through_reauth(
    hass, entry, gate_clock, budget_transport, real_clients
):
    from custom_components.haven.runtime import async_get_account_store

    accounts = await async_get_account_store(hass)
    gate = await accounts.async_get_gate(entry.data["email"])
    gate.min_request_interval = 0
    budget_transport.login_status = 400

    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await async_drain_scheduled_work(hass)
        assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
        failed_client = hass.data["haven"][entry.entry_id]["client"]
        assert hass.data["haven"][entry.entry_id]["authentication_required"]
        assert len(budget_transport.calls) == 1
        assert len(budget_transport.login_calls) == 1
        assert gate.rate_limit_remaining == 0

        # A rejected sign-in needs user action, not another unchanged login.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
        await async_drain_scheduled_work(hass)
        assert len(budget_transport.calls) == 1
        reauth_flows = [
            flow for flow in hass.config_entries.flow.async_progress()
            if flow["context"].get("entry_id") == entry.entry_id
            and flow["context"].get("source") == config_entries.SOURCE_REAUTH
        ]
        assert len(reauth_flows) == 1
        flow_id = reauth_flows[0]["flow_id"]

        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input={"password": "still-rejected-password"}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "login_rejected"}
        assert entry.data["password"] == "test-password"
        assert len(budget_transport.login_calls) == 2
        assert real_clients.clients[-1].closed
        assert not failed_client.closed

        budget_transport.login_status = 200
        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input={"password": "replacement-password"}
        )
        await async_drain_scheduled_work(hass)
        assert result["type"] == "abort"
        assert result["reason"] == "reauth_successful"
        assert entry.state is config_entries.ConfigEntryState.LOADED
        assert entry.data["password"] == "replacement-password"
        assert len(hass.config_entries.async_entries("haven")) == 1

        # HA does not run the integration unload hook for failed initial setup.
        # Its retained client must authenticate with the newly stored password.
        runtime = hass.data["haven"][entry.entry_id]
        assert runtime["client"] is failed_client
        assert not runtime["authentication_required"]
        assert failed_client.is_authenticated
        assert len(real_clients.clients) == 3
        assert all(client.closed for client in real_clients.clients[1:])
        assert all(client.request_gate is gate for client in real_clients.clients)
        assert len(budget_transport.login_calls) == 4
        assert budget_transport.login_calls[-1][0] is failed_client._credentials._session
        assert budget_transport.login_calls[-1][1]["Password"] == "replacement-password"
        assert hass.states.get(entity_for(hass)).state == "off"
    finally:
        await hass.config_entries.async_remove(entry.entry_id)
        await async_drain_scheduled_work(hass)


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [(400, "login_rejected"), (429, "cannot_connect"), (503, "cannot_connect")],
)
async def test_setup_form_distinguishes_login_rejection_from_provider_cooldown(
    hass, gate_clock, budget_transport, real_clients, status, expected_error
):
    from custom_components.haven.runtime import async_get_account_store

    email = "new-account@example.invalid"
    accounts = await async_get_account_store(hass)
    gate = await accounts.async_get_gate(email)
    gate.min_request_interval = 0
    budget_transport.login_status = status
    budget_transport.login_retry_after = 3600 if status in (429, 503) else None
    form = await hass.config_entries.flow.async_init(
        "haven", context={"source": config_entries.SOURCE_USER}
    )

    for password in ("test-password", "replacement-password"):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={"email": email, "password": password}
        )
        assert result["type"] == "form"
        assert result["errors"] == {"base": expected_error}
        assert real_clients.clients[-1].closed
        assert real_clients.clients[-1].request_gate is gate

    assert hass.config_entries.async_entries("haven") == []
    if status == 400:
        assert len(budget_transport.login_calls) == 2
        assert gate.rate_limit_remaining == 0
    else:
        assert len(budget_transport.login_calls) == 1
        assert gate.rate_limit_remaining == 3600
    hass.config_entries.flow.async_abort(form["flow_id"])


@pytest.mark.parametrize("status", [200, 202])
async def test_empty_command_success_completes_color_batch_and_debounced_refresh(
    hass, real_loaded_entry, haven_api, budget_transport, status
):
    from custom_components.haven.const import COMMAND_REFRESH_DELAY

    entity_id = entity_for(hass)
    assert hass.states.get(entity_id).state == "off"
    budget_transport.command_success_status = status
    budget_transport.command_response_body = b""
    before = len(budget_transport.calls)

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)},
        blocking=True,
    )
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
    ]
    assert haven_api.zones[0]["colorId"] == 25
    assert haven_api.zones[0]["isOn"] is True
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["rgb_color"] == (0, 0, 255)

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=COMMAND_REFRESH_DELAY + 1)
    )
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"), ("POST", "/Commands/On"),
        ("GET", f"/LightAndZones/OrderedList/{LOCATION_ID}"),
        ("GET", f"/Group/AllGroupsByLocation/{LOCATION_ID}"),
    ]
    assert [call[1] for call in haven_api.command_calls] == [
        "/Commands/SetColor", "/Commands/On",
    ]
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["rgb_color"] == (0, 0, 255)


async def test_malformed_command_reply_stops_batch_without_confirmation_or_replay(
    hass, real_loaded_entry, haven_api, budget_transport
):
    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    previous = coordinator.data
    key = next(key for key in previous if key.kind == "Zone" and key.remote_id == 101)
    assert previous[key].color_id == 1
    assert previous[key].is_on is False
    budget_transport.command_response_body = b'{"truncated":'
    before = len(budget_transport.calls)
    entity_id = entity_for(hass)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)},
            blocking=True,
        )
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"),
    ]
    assert haven_api.zones[0]["colorId"] == 25
    assert haven_api.zones[0]["isOn"] is False
    assert coordinator.data == previous
    assert hass.states.get(entity_id).state == "off"

    # Reconcile the provider state with reads, never replay the ambiguous POST.
    await coordinator.async_refresh()
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/SetColor"),
        ("GET", f"/LightAndZones/OrderedList/{LOCATION_ID}"),
        ("GET", f"/Group/AllGroupsByLocation/{LOCATION_ID}"),
    ]
    assert [call[1] for call in haven_api.command_calls] == ["/Commands/SetColor"]
    assert coordinator.data[key].color_id == 25
    assert coordinator.data[key].is_on is False


async def test_unchanged_command_failure_keeps_pending_refresh(
    hass, real_loaded_entry, haven_api, budget_transport
):
    """A failed reply must not cancel a previously scheduled reconciliation."""
    from custom_components.haven.const import COMMAND_REFRESH_DELAY

    coordinator = hass.data["haven"][real_loaded_entry.entry.entry_id]["coordinators"][0]
    entity_id = entity_for(hass)
    before = len(budget_transport.calls)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    previous = coordinator.data
    budget_transport.command_response_body = b'{"truncated":'
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)},
            blocking=True,
        )
    await async_drain_scheduled_work(hass)
    assert coordinator.data == previous
    assert haven_api.zones[0]["colorId"] == 25
    assert len(budget_transport.calls) == before + 2

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=COMMAND_REFRESH_DELAY + 1)
    )
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/On"), ("POST", "/Commands/SetColor"),
        ("GET", f"/LightAndZones/OrderedList/{LOCATION_ID}"),
        ("GET", f"/Group/AllGroupsByLocation/{LOCATION_ID}"),
    ]
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes["rgb_color"] == (0, 0, 255)


@pytest.mark.parametrize("status", [429, 503])
async def test_unchanged_command_failure_does_not_bypass_cooldown(
    hass, real_loaded_entry, budget_transport, status
):
    """Keeping a pending read never sends HTTP through a provider cooldown."""
    from custom_components.haven.const import COMMAND_REFRESH_DELAY

    context = real_loaded_entry
    entity_id = entity_for(hass)
    before = len(budget_transport.calls)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    budget_transport.command_status = status
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": entity_id, "rgb_color": (0, 0, 255)},
            blocking=True,
        )
    retry_at = context.gate.snapshot()["retry_at"]
    assert context.gate.rate_limit_remaining == 3600

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=COMMAND_REFRESH_DELAY + 1)
    )
    await async_drain_scheduled_work(hass)
    assert [(method, path) for _session, method, path in budget_transport.calls[before:]] == [
        ("POST", "/Commands/On"), ("POST", "/Commands/SetColor"),
    ]
    assert context.gate.snapshot()["retry_at"] == retry_at
