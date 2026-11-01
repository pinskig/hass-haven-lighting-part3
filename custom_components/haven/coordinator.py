"""Poll each Haven location once and share its data with every entity."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from datetime import timedelta
import logging
import time

from homeassistant import const as ha_const

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import COMMAND_REFRESH_DELAY, DEFAULT_POLL_INTERVAL
from .havenlighting.devices.location import Location
from .havenlighting.exceptions import ApiError, AuthenticationError
from .havenlighting.models import LightState, TargetKey
from .havenlighting.request_context import get_request_trace, new_request_trace, request_trace
from .havenlighting.version import INTEGRATION_VERSION
from .runtime import async_get_account_store
from .trace import (
    command_description, sanitize_action_description, state_description, target_reference,
)

_LOGGER = logging.getLogger(__name__)


class HavenCoordinator(DataUpdateCoordinator[dict[TargetKey, LightState]]):
    """Own the scheduled and debounced updates for one location."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, location: Location, account_id: str
    ) -> None:
        self.location = location
        self.account_id = account_id
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation = 0
        self._last_refresh_error: BaseException | None = None
        self._last_refresh_data: dict[TargetKey, LightState] = {}
        # Constructed during integration setup: HA binds the current config entry.
        # Omitting the newer config_entry keyword preserves HA 2024.1 support.
        super().__init__(
            hass,
            _LOGGER,
            name=f"Haven {location.location_id}",
            update_interval=DEFAULT_POLL_INTERVAL,
            always_update=False,
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=COMMAND_REFRESH_DELAY,
                immediate=False,
            ),
        )
        self.config_entry = entry

    async def _async_update_data(self) -> dict[TargetKey, LightState]:
        # Also serialize explicit refreshes on older HA coordinator versions.
        generation = self._refresh_generation
        async with self._refresh_lock:
            if generation != self._refresh_generation:
                if self._last_refresh_error is not None:
                    raise self._last_refresh_error
                return self._last_refresh_data
            try:
                data = await self._async_fetch_data()
                self._last_refresh_data = data
                self._last_refresh_error = None
                return data
            except (Exception, asyncio.CancelledError) as error:
                self._last_refresh_error = error
                raise
            finally:
                self._refresh_generation += 1

    async def _async_fetch_data(self) -> dict[TargetKey, LightState]:
        trace_id = new_request_trace()
        previous = self.data or {}
        started = time.monotonic()
        _LOGGER.debug("Haven poll: phase=started trace=%s", trace_id)
        try:
            with request_trace(trace_id):
                data = await self.hass.async_add_executor_job(
                    copy_context().run, self.location.refresh_and_snapshot, True
                )
        except AuthenticationError as error:
            _LOGGER.debug("Haven poll: phase=failed trace=%s category=authentication", trace_id)
            raise ConfigEntryAuthFailed(
                "Haven authentication expired; check the integration credentials"
            ) from error
        except ApiError as error:
            _LOGGER.debug(
                "Haven poll: phase=failed trace=%s category=api status=%s local_cooldown=%s",
                trace_id, error.code, error.rate_limited_locally,
            )
            accounts = await async_get_account_store(self.hass)
            await accounts.async_flush()
            delay = max(
                DEFAULT_POLL_INTERVAL.total_seconds(), error.retry_after or 0
            )
            self.update_interval = timedelta(seconds=delay)
            failure = UpdateFailed(str(error))
            # Current HA honors this for setup and scheduled retries. Older
            # versions use update_interval; Credentials still gates all HTTP.
            failure.retry_after = delay
            raise failure from error

        self.update_interval = DEFAULT_POLL_INTERVAL
        if _LOGGER.isEnabledFor(logging.DEBUG):
            for key in previous.keys() | data.keys():
                _LOGGER.debug(
                    "Haven poll state: trace=%s target=%s before=%s reported=%s",
                    trace_id, target_reference(self.account_id, key),
                    state_description(previous.get(key)), state_description(data.get(key)),
                )
            _LOGGER.debug(
                "Haven poll: phase=completed trace=%s elapsed=%.3fs targets=%s source=inventory",
                trace_id, time.monotonic() - started, len(data),
            )
        return data

    async def async_send_commands(
        self, key: TargetKey, commands: list[tuple[str, tuple]], *,
        requested: dict | None = None,
    ) -> None:
        """Publish acknowledged command state and schedule one shared refresh."""
        trace_id = get_request_trace()
        if trace_id == "-":
            trace_id = new_request_trace()
        received_at = time.monotonic()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Haven action: phase=received trace=%s target=%s requested=%s mapped=%s "
                "ha=%s version=%s",
                trace_id, target_reference(self.account_id, key),
                sanitize_action_description(requested), command_description(commands),
                getattr(ha_const, "__version__", "unknown"), INTEGRATION_VERSION,
            )
        with request_trace(trace_id):
            async with self._refresh_lock:
                before = (self.data or {}).get(key)
                started = time.monotonic()
                _LOGGER.debug(
                    "Haven action: phase=started trace=%s queue_wait=%.3fs",
                    trace_id, started - received_at,
                )
                try:
                    data = await self.hass.async_add_executor_job(
                        copy_context().run, self.location.execute_commands, key, commands
                    )
                except (ApiError, AuthenticationError) as error:
                    # Acknowledgments permit optimistic state, not confirmation
                    # of physical device state. Keep accepted partial changes.
                    data = self.location.snapshot
                    self._last_refresh_data = data
                    # Do not cancel reconciliation or postpone the normal poll
                    # when a failed batch has changed only part of the state.
                    if self.last_update_success and data != self.data:
                        self.data = data
                        self.async_update_listeners()
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Haven action: phase=error trace=%s target=%s status=%s "
                            "local_cooldown=%s elapsed=%.3fs before=%s after=%s source=optimistic",
                            trace_id, target_reference(self.account_id, key), error.code,
                            getattr(error, "rate_limited_locally", False),
                            time.monotonic() - started,
                            state_description(before), state_description(data.get(key)),
                        )
                    accounts = await async_get_account_store(self.hass)
                    await accounts.async_flush()
                    raise HomeAssistantError(str(error)) from error
                self._last_refresh_data = data
                self.async_set_updated_data(data)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Haven action: phase=accepted trace=%s target=%s elapsed=%.3fs "
                        "before=%s after=%s source=optimistic",
                        trace_id, target_reference(self.account_id, key),
                        time.monotonic() - started,
                        state_description(before), state_description(data.get(key)),
                    )
            await self.async_request_refresh()
