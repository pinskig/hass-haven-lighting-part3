"""Account-scoped request pacing and provider cooldowns.

The gate outlives individual clients, setup flows, and integration reloads.
Persistence contains only a retry deadline/status and backoff counter, never
credentials. Wall-clock deadlines are converted to monotonic time on restore.
"""

from collections.abc import Callable, Mapping
import logging
import math
import threading
import time
from typing import Any

from .exceptions import ApiError

_LOGGER = logging.getLogger(__name__)


class RequestGate:
    """Serialize all requests for an account and preserve its provider budget."""

    DEFAULT_REQUEST_INTERVAL = 1.0
    RATE_LIMIT_BACKOFF_STEPS = (60, 300, 900)
    STABLE_SUCCESS_THRESHOLD = 3
    # Latest representable UTC timestamp (9999-12-31). This bounds malformed
    # persistence and extreme provider values without shortening normal delays.
    MAX_RETRY_AT = 253402300799.0

    def __init__(
        self,
        min_request_interval: float = DEFAULT_REQUEST_INTERVAL,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if (
            not isinstance(min_request_interval, (int, float))
            or not math.isfinite(min_request_interval)
            or min_request_interval < 0
        ):
            raise ValueError("min_request_interval must be finite and nonnegative")
        self.lock = threading.RLock()
        self.min_request_interval = float(min_request_interval)
        self.on_change = on_change
        self._next_request_at = 0.0
        self._retry_until = 0.0
        self._status: int | None = None
        self._consecutive_429 = 0
        self._consecutive_successes = 0

    @property
    def rate_limit_remaining(self) -> int:
        """Return seconds until requests are permitted, rounded up."""
        with self.lock:
            return max(0, math.ceil(self._retry_until - time.monotonic()))

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe provider budget state, with no account secrets."""
        with self.lock:
            remaining = max(0.0, self._retry_until - time.monotonic())
            return {
                "retry_at": min(self.MAX_RETRY_AT, time.time() + remaining)
                if remaining else None,
                "status": self._status if remaining else None,
                "consecutive_429": self._consecutive_429,
            }

    def restore(self, state: Mapping[str, Any]) -> bool:
        """Merge valid persisted state without shortening an existing cooldown.

        Invalid or expired deadlines never clear the current gate. The return
        value lets the storage owner report malformed persistence if desired.
        """
        if not isinstance(state, Mapping):
            return False
        retry_at = state.get("retry_at")
        status = state.get("status")
        count = state.get("consecutive_429", 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000:
            return False
        if retry_at is None:
            if status not in (None, 0):
                return False
            remaining = 0.0
        else:
            if (
                isinstance(retry_at, bool)
                or not isinstance(retry_at, (int, float))
                or not 0 <= retry_at <= self.MAX_RETRY_AT
                or not math.isfinite(retry_at)
                or status not in (429, 503)
            ):
                return False
            remaining = max(0.0, float(retry_at) - time.time())

        with self.lock:
            self._consecutive_429 = max(self._consecutive_429, count)
            if remaining:
                deadline = time.monotonic() + remaining
                if deadline > self._retry_until:
                    self._retry_until = deadline
                    self._status = status
            return True

    def raise_if_blocked(self) -> None:
        """Reject locally without extending the server's deadline."""
        with self.lock:
            remaining = self.rate_limit_remaining
            if remaining:
                raise ApiError(
                    f"Haven API cooldown active; retry in {remaining} seconds",
                    code=self._status or 429,
                    retry_after=remaining,
                    rate_limited_locally=True,
                )

    def before_request(self) -> None:
        """Apply a short spacing delay; callers hold lock throughout HTTP."""
        with self.lock:
            self.raise_if_blocked()
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.raise_if_blocked()
            self._next_request_at = time.monotonic() + self.min_request_interval

    def record_rate_limit(self, retry_after: int | None) -> int:
        """Escalate only an actual HTTP 429, never a locally rejected attempt."""
        with self.lock:
            self._consecutive_429 += 1
            self._consecutive_successes = 0
            step = min(self._consecutive_429 - 1, len(self.RATE_LIMIT_BACKOFF_STEPS) - 1)
            delay = max(self.RATE_LIMIT_BACKOFF_STEPS[step], retry_after or 0)
            return self._defer(429, delay)

    def record_service_unavailable(self, retry_after: int) -> int:
        """Honor a 503 deadline without increasing the 429 escalation counter."""
        with self.lock:
            self._consecutive_successes = 0
            return self._defer(503, retry_after)

    def _defer(self, status: int, delay: int | float) -> int:
        delay = min(delay, max(0.0, self.MAX_RETRY_AT - time.time()))
        deadline = time.monotonic() + delay
        if deadline >= self._retry_until:
            self._retry_until = deadline
            self._status = status
        self._notify_changed()
        return self.rate_limit_remaining

    def record_error(self) -> None:
        """Require consecutive successes before resetting 429 escalation."""
        with self.lock:
            self._consecutive_successes = 0

    def record_success(self) -> None:
        with self.lock:
            self._consecutive_successes += 1
            if self._consecutive_429 and self._consecutive_successes >= self.STABLE_SUCCESS_THRESHOLD:
                self._consecutive_429 = 0
                self._notify_changed()

    def _notify_changed(self) -> None:
        """Notify the owner; the callback should only schedule persistence."""
        callback = self.on_change
        if callback is not None:
            try:
                callback(self.snapshot())
            except Exception:
                _LOGGER.exception("Unable to schedule Haven request-budget persistence")
