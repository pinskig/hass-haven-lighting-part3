"""The Haven Lighting integration."""
from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import HavenCoordinator
from .havenlighting import HavenClient
from .havenlighting.exceptions import ApiError, AuthenticationError
from .runtime import async_get_account_store
from .registry import async_prepare_registries

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SELECT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Haven Lighting from a config entry."""
    accounts = await async_get_account_store(hass)
    if not accounts.claim_account(entry.data["email"], entry.entry_id):
        raise ConfigEntryError(
            "This Haven account is already loaded by another entry; remove the duplicate entry"
        )
    gate = await accounts.async_get_gate(entry.data["email"])
    # Keep authenticated sessions across automatic setup retries. The separate
    # account gate also preserves cooldowns when a client must be replaced.
    entries = hass.data.setdefault(DOMAIN, {})
    if entry.entry_id not in entries:
        client = HavenClient(request_gate=gate)
        accounts.track_client(client)
        entries[entry.entry_id] = {"client": client, "coordinators": []}
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
        if client.account_id is None:
            raise AuthenticationError("Haven did not return a valid account identifier")
    except AuthenticationError as error:
        runtime["authentication_required"] = True
        raise ConfigEntryAuthFailed(str(error)) from error
    except ApiError as error:
        await accounts.async_flush()
        failure = ConfigEntryNotReady(str(error))
        failure.retry_after = error.retry_after
        raise failure from error

    coordinators = []
    try:
        for location in locations.values():
            coordinator = HavenCoordinator(hass, entry, location, client.account_id)
            coordinators.append(coordinator)
            await coordinator.async_config_entry_first_refresh()
        runtime["coordinators"] = coordinators
        if coordinators:
            # Fail at the config-entry boundary if migration detects a registry
            # conflict. A platform setup failure alone can otherwise be logged
            # by HA while the parent entry still appears successfully loaded.
            async_prepare_registries(hass, entry, coordinators, set())
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError) as error:
        if isinstance(error, ConfigEntryAuthFailed):
            runtime["authentication_required"] = True
        for coordinator in coordinators:
            await coordinator.async_shutdown()
        runtime["coordinators"] = []
        if isinstance(error, ConfigEntryError) and not isinstance(error, ConfigEntryAuthFailed):
            entries.pop(entry.entry_id, None)
            await accounts.async_close_client(client)
            accounts.release_account(entry.data["email"], entry.entry_id)
        raise
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        for coordinator in runtime["coordinators"]:
            await coordinator.async_shutdown()
        accounts = await async_get_account_store(hass)
        await accounts.async_close_client(runtime["client"])
        accounts.release_account(entry.data["email"], entry.entry_id)
        await accounts.async_flush()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release a failed setup's client without clearing its account cooldown."""
    accounts = await async_get_account_store(hass)
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        for coordinator in runtime["coordinators"]:
            await coordinator.async_shutdown()
        await accounts.async_close_client(runtime["client"])
    accounts.release_account(entry.data["email"], entry.entry_id)
    await accounts.async_flush()
