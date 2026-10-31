"""The Haven Lighting integration."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting import HavenClient
from .havenlighting.exceptions import ApiError, AuthenticationError

PLATFORMS: list[Platform] = [Platform.LIGHT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Haven Lighting from a config entry."""
    # Keep the same client across automatic setup retries. Recreating it would
    # discard the shared cooldown after a rate-limited login or discovery call.
    entries = hass.data.setdefault(DOMAIN, {})
    if entry.entry_id not in entries:
        entries[entry.entry_id] = {"client": HavenClient(), "coordinators": []}
    runtime = entries[entry.entry_id]
    client: HavenClient = runtime["client"]

    try:
        if runtime.get("authentication_required") or not client.is_authenticated:
            authenticated = await hass.async_add_executor_job(
                client.authenticate, entry.data["email"], entry.data["password"]
            )
            if not authenticated:
                runtime["authentication_required"] = True
                raise ConfigEntryAuthFailed("Unable to authenticate with Haven")
            runtime["authentication_required"] = False
        locations = await hass.async_add_executor_job(client.discover_locations)
    except AuthenticationError as error:
        runtime["authentication_required"] = True
        raise ConfigEntryAuthFailed(str(error)) from error
    except ApiError as error:
        failure = ConfigEntryNotReady(str(error))
        failure.retry_after = error.retry_after
        raise failure from error

    coordinators = []
    try:
        for location in locations.values():
            coordinator = HavenCoordinator(hass, entry, location)
            coordinators.append(coordinator)
            await coordinator.async_config_entry_first_refresh()
        runtime["coordinators"] = coordinators
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError) as error:
        if isinstance(error, ConfigEntryAuthFailed):
            runtime["authentication_required"] = True
        for coordinator in coordinators:
            await coordinator.async_shutdown()
        runtime["coordinators"] = []
        raise
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        for coordinator in runtime["coordinators"]:
            await coordinator.async_shutdown()

    return unload_ok
