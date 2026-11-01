"""Action intent and HTTP correlation through actual HA service calls."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import re

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
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


_PREFIXES = (
    "Haven action:", "Haven HTTP attempt:", "Haven poll:",
    "Haven poll state:", "Haven color configuration:",
)


def diagnostics(caplog):
    return [record for record in caplog.records
            if record.name.startswith("custom_components.haven")
            and record.getMessage().startswith(_PREFIXES)]


def one_record(records, prefix, phase=None):
    matching = [record for record in records
                if record.getMessage().startswith(prefix)
                and (phase is None or f"phase={phase} " in record.getMessage())]
    assert len(matching) == 1, [record.getMessage() for record in matching]
    return matching[0]


def trace(record):
    match = re.search(r"\btrace=([0-9a-f]{12})(?:\s|$)", record.getMessage())
    assert match is not None, record.getMessage()
    return match.group(1)


def state_arguments(record):
    return [value for value in record.args
            if isinstance(value, dict) and "color_id" in value]


async def test_color_action_logs_intent_optimistic_result_and_separate_poll(
    hass, real_loaded_entry, budget_transport, haven_api, hass_admin_user, caplog
):
    from custom_components.haven.const import COMMAND_REFRESH_DELAY
    from custom_components.haven.trace import private_reference

    entity_id = entity_for(hass)
    context = Context(
        id="01K8X4J9QQ9A9H37A3MFQGQYV8",
        parent_id="01K8X4J9QQP43Z6YNGA8D6TWJ3",
        user_id=hass_admin_user.id,
    )
    budget_transport.command_response_body = b""
    caplog.set_level(logging.DEBUG, logger="custom_components.haven")
    caplog.clear()
    before_calls = len(budget_transport.calls)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, "color_temp_kelvin": 3200},
        blocking=True, context=context,
    )
    await async_drain_scheduled_work(hass)

    records = diagnostics(caplog)
    received = one_record(records, "Haven action:", "received")
    started = one_record(records, "Haven action:", "started")
    accepted = one_record(records, "Haven action:", "accepted")
    action_trace = trace(received)
    assert trace(started) == trace(accepted) == action_trace
    # Minimum HA bypasses async_request_call for a single entity. Current HA
    # exposes this boundary, including the wait on its unchanged semaphore.
    if not HA_VERSION.startswith("2024.1."):
        service_received = one_record(records, "Haven action:", "service_received")
        service_finished = one_record(records, "Haven action:", "service_finished")
        assert trace(service_received) == trace(service_finished) == action_trace
        assert records.index(service_received) < records.index(received)
        assert records.index(accepted) < records.index(service_finished)
        elapsed = re.search(r"elapsed=([0-9]+\.[0-9]{3})s", service_finished.getMessage())
        assert elapsed is not None and float(elapsed.group(1)) >= 0
    wait = re.search(r"queue_wait=([0-9]+\.[0-9]{3})s", started.getMessage())
    assert wait is not None and float(wait.group(1)) >= 0

    requested = next(value for value in received.args if isinstance(value, dict))
    mapped = next(value for value in received.args if isinstance(value, list))
    assert requested["mode"] == "color_temp"
    assert requested["kelvin"] == 3200
    assert requested["has_user"] is True
    assert re.fullmatch(r"[0-9a-f]{12}", requested["context"])
    assert re.fullmatch(r"[0-9a-f]{12}", requested["parent"])
    assert requested["context"] == private_reference(context.id)
    assert requested["parent"] == private_reference(context.parent_id)
    assert requested["context"] != requested["parent"]
    assert mapped == [("set_color", (2,)), ("turn_on", ())]

    before, after = state_arguments(accepted)
    assert before["on"] is False and before["color_id"] == 1
    assert after["on"] is True and after["color_id"] == 2
    assert after["kelvin"] == 3000
    assert "source=optimistic" in accepted.getMessage()
    attempts = [record for record in records if record.getMessage().startswith("Haven HTTP attempt:")]
    assert len(attempts) == 2
    assert all(trace(record) == action_trace for record in attempts)
    assert "endpoint=/Commands/SetColor" in attempts[0].getMessage()
    assert "endpoint=/Commands/On" in attempts[1].getMessage()
    assert all("response_bytes=0" in record.getMessage() for record in attempts)
    assert len(budget_transport.calls) == before_calls + 2

    # A later read can disagree with the optimistic acknowledgment. Diagnostics
    # must identify this as a separate inventory observation, with its own trace.
    haven_api.zones[0]["colorId"] = 25
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=COMMAND_REFRESH_DELAY + 1)
    )
    await async_drain_scheduled_work(hass)
    records = diagnostics(caplog)
    poll_start = one_record(records, "Haven poll:", "started")
    poll_complete = one_record(records, "Haven poll:", "completed")
    poll_state = one_record(records, "Haven poll state:")
    poll_trace = trace(poll_start)
    assert poll_trace != action_trace
    assert trace(poll_complete) == trace(poll_state) == poll_trace
    assert "source=inventory" in poll_complete.getMessage()
    optimistic, reported = state_arguments(poll_state)
    assert optimistic["color_id"] == 2
    assert reported["color_id"] == 25 and reported["rgb"] == (0, 0, 255)
    poll_attempts = [record for record in records
                     if record.getMessage().startswith("Haven HTTP attempt:")
                     and "method=GET" in record.getMessage()]
    assert len(poll_attempts) == 2
    assert all(trace(record) == poll_trace for record in poll_attempts)
    assert len(budget_transport.calls) == before_calls + 4

    messages = "\n".join(record.getMessage() for record in records)
    references = re.findall(r"\btarget=([0-9a-f]{12})(?:\s|$)", messages)
    assert references and len(set(references)) == 1
    for private in (
        context.id, context.parent_id, context.user_id, entity_id,
        "Front yard", "Test location", "test@example.invalid", "test-password",
        "test-access-token", "test-refresh-token",
        f"haven:{ACCOUNT_ID}:{LOCATION_ID}:zone:101",
    ):
        assert private not in messages
    for raw_field in ("account_id=", "location_id=", "remote_id=", "user_id="):
        assert raw_field not in messages


async def test_explicit_command_failure_logs_sanitized_action_error(
    hass, real_loaded_entry, budget_transport, caplog
):
    budget_transport.command_response_body = json.dumps([
        {"success": False, "detail": "PRIVATE_RESPONSE_BODY", "token": "PRIVATE_TOKEN"}
    ]).encode()
    caplog.set_level(logging.DEBUG, logger="custom_components.haven")
    caplog.clear()
    before_calls = len(budget_transport.calls)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on",
            {"entity_id": entity_for(hass), "rgb_color": (0, 0, 255)}, blocking=True,
        )
    await async_drain_scheduled_work(hass)
    records = diagnostics(caplog)
    received = one_record(records, "Haven action:", "received")
    failed = one_record(records, "Haven action:", "error")
    http = one_record(records, "Haven HTTP attempt:")
    assert trace(received) == trace(failed) == trace(http)
    assert "local_cooldown=False" in failed.getMessage()
    assert "source=optimistic" in failed.getMessage()
    before, after = state_arguments(failed)
    assert before == after
    assert before["on"] is False and before["color_id"] == 1
    assert "status=200" in http.getMessage()
    assert not any("phase=accepted" in record.getMessage() for record in records)
    assert len(budget_transport.calls) == before_calls + 1
    assert budget_transport.calls[-1][2] == "/Commands/SetColor"
    assert "PRIVATE_" not in "\n".join(record.getMessage() for record in records)


async def test_local_cooldown_logs_action_error_without_http_attempt(
    hass, real_loaded_entry, budget_transport, caplog
):
    budget_transport.command_status = 429
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": entity_for(hass)}, blocking=True
        )
    caplog.set_level(logging.DEBUG, logger="custom_components.haven")
    caplog.clear()
    before_calls = len(budget_transport.calls)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": entity_for(hass)}, blocking=True
        )
    await async_drain_scheduled_work(hass)
    records = diagnostics(caplog)
    received = one_record(records, "Haven action:", "received")
    failed = one_record(records, "Haven action:", "error")
    assert trace(received) == trace(failed)
    assert "status=429" in failed.getMessage()
    assert "local_cooldown=True" in failed.getMessage()
    assert not any(record.getMessage().startswith("Haven HTTP attempt:") for record in records)
    assert len(budget_transport.calls) == before_calls


async def test_info_level_does_not_emit_action_diagnostics(
    hass, real_loaded_entry, budget_transport, caplog
):
    caplog.set_level(logging.INFO, logger="custom_components.haven")
    caplog.clear()
    before_calls = len(budget_transport.calls)
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_for(hass)}, blocking=True
    )
    await async_drain_scheduled_work(hass)
    assert len(budget_transport.calls) == before_calls + 1
    assert diagnostics(caplog) == []
