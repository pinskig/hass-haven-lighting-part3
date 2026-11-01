"""Account request budgets that outlive entries, flows, and HA restarts."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .havenlighting import HavenClient
from .havenlighting.request_gate import RequestGate

_ACCOUNTS_KEY = f"{DOMAIN}_accounts"


def account_key(email: str) -> str:
    """Keep normalized account addresses out of the cooldown storage file."""
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()


class AccountGateStore:
    """Keep one budget per account, independently of any HTTP session."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, 1, f"{DOMAIN}.request_budgets")
        self._load_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._loaded = False
        self._gates: dict[str, RequestGate] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._clients: set[HavenClient] = set()
        self._owners: dict[str, str] = {}
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_shutdown)

    async def async_load(self) -> None:
        async with self._load_lock:
            if self._loaded:
                return
            stored = await self._store.async_load()
            if isinstance(stored, dict) and isinstance(stored.get("accounts"), dict):
                self._snapshots = {
                    key: value
                    for key, value in stored["accounts"].items()
                    if isinstance(key, str) and isinstance(value, dict)
                }
            self._loaded = True

    async def async_get_gate(self, email: str) -> RequestGate:
        await self.async_load()
        key = account_key(email)
        if key not in self._gates:
            gate = RequestGate()
            gate.restore(self._snapshots.get(key, {}))
            # This callback runs under a worker-thread transport lock. Never
            # acquire that lock from the event loop to read a live snapshot.
            gate.on_change = lambda snapshot: self.hass.loop.call_soon_threadsafe(
                self._record_snapshot, key, snapshot
            )
            self._gates[key] = gate
        return self._gates[key]

    @callback
    def claim_account(self, email: str, entry_id: str) -> bool:
        key = account_key(email)
        owner = self._owners.setdefault(key, entry_id)
        return owner == entry_id

    @callback
    def release_account(self, email: str, entry_id: str) -> None:
        key = account_key(email)
        if self._owners.get(key) == entry_id:
            self._owners.pop(key)

    @callback
    def track_client(self, client: HavenClient) -> None:
        self._clients.add(client)

    async def async_close_client(self, client: HavenClient) -> None:
        self._clients.discard(client)
        await self.hass.async_add_executor_job(client.close)

    @callback
    def _record_snapshot(self, key: str, snapshot: dict[str, Any]) -> None:
        self._snapshots[key] = dict(snapshot)
        self._store.async_delay_save(self._serialize, 1)

    @callback
    def _serialize(self) -> dict[str, Any]:
        return {"accounts": {key: dict(value) for key, value in self._snapshots.items()}}

    async def async_flush(self) -> None:
        """Persist completed request outcomes before returning setup errors."""
        # Allow callbacks posted by the just-completed executor job to run.
        await asyncio.sleep(0)
        async with self._save_lock:
            await self._store.async_save(self._serialize())

    async def _async_shutdown(self, _event: Any) -> None:
        clients = tuple(self._clients)
        if clients:
            await asyncio.gather(*(self.async_close_client(client) for client in clients))
        await self.async_flush()


async def async_get_account_store(hass: HomeAssistant) -> AccountGateStore:
    if _ACCOUNTS_KEY not in hass.data:
        hass.data[_ACCOUNTS_KEY] = AccountGateStore(hass)
    store: AccountGateStore = hass.data[_ACCOUNTS_KEY]
    await store.async_load()
    return store
