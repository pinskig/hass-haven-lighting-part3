"""Configure and reauthenticate a Haven account without duplicate polling."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .havenlighting import HavenClient
from .havenlighting.exceptions import ApiError, AuthenticationError
from .runtime import async_get_account_store

_LOGGER = logging.getLogger(__name__)


class HavenConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup and expired credentials for one Haven account."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def _async_authenticate(self, data: dict[str, Any]) -> dict[str, str]:
        accounts = await async_get_account_store(self.hass)
        gate = await accounts.async_get_gate(data[CONF_EMAIL])
        client = HavenClient(request_gate=gate)
        accounts.track_client(client)
        try:
            authenticated = await self.hass.async_add_executor_job(
                client.authenticate, data[CONF_EMAIL], data[CONF_PASSWORD]
            )
            return {} if authenticated else {"base": "invalid_auth"}
        except AuthenticationError:
            return {"base": "invalid_auth"}
        except ApiError as error:
            _LOGGER.warning("Haven setup request failed: %s", error)
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected Haven setup error")
            return {"base": "unknown"}
        finally:
            # Every flow outcome closes its pool. Its account budget survives
            # resubmission, a new flow, reload, and process restart.
            await accounts.async_close_client(client)
            await accounts.async_flush()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_EMAIL] = user_input[CONF_EMAIL].strip()
            account = user_input[CONF_EMAIL].casefold()
            # Also recognize older entries that predate unique IDs.
            if any(
                entry.data.get(CONF_EMAIL, "").strip().casefold() == account
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(account)
            self._abort_if_unique_id_configured()
            errors = await self._async_authenticate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        errors = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors = await self._async_authenticate(data)
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )
