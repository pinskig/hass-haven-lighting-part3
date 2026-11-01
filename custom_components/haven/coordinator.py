"""Poll each Haven location once and share its data with every entity."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import COMMAND_REFRESH_DELAY, DEFAULT_POLL_INTERVAL
from .havenlighting.devices.location import Location
from .havenlighting.exceptions import ApiError, AuthenticationError
from .havenlighting.models import LightState, TargetKey
from .runtime import async_get_account_store

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
        try:
            data = await self.hass.async_add_executor_job(
                self.location.refresh_and_snapshot, True
            )
        except AuthenticationError as error:
            raise ConfigEntryAuthFailed(
                "Haven authentication expired; check the integration credentials"
            ) from error
        except ApiError as error:
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
        return data

    async def async_send_commands(
        self, key: TargetKey, commands: list[tuple[str, tuple]]
    ) -> None:
        """Publish successful command state and schedule one shared refresh."""
        async with self._refresh_lock:
            try:
                data = await self.hass.async_add_executor_job(
                    self.location.execute_commands, key, commands
                )
            except (ApiError, AuthenticationError) as error:
                # A batch can partly succeed. Preserve those confirmed changes
                # while surfacing the failure; no automatic command retry.
                data = self.location.snapshot
                self._last_refresh_data = data
                # Publish confirmed partial changes without canceling a pending
                # reconciliation or postponing the normal poll after a failure.
                if self.last_update_success and data != self.data:
                    self.data = data
                    self.async_update_listeners()
                accounts = await async_get_account_store(self.hass)
                await accounts.async_flush()
                raise HomeAssistantError(str(error)) from error
            self._last_refresh_data = data
            self.async_set_updated_data(data)
        await self.async_request_refresh()
