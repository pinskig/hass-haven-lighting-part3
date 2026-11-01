"""Authentication and a persistent HTTP session behind an account request gate."""

from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import logging
import math
import time
from typing import Any
import uuid

import requests

from .config import ApiConfig
from .exceptions import AuthenticationError, ApiError
from .request_gate import RequestGate

logger = logging.getLogger(__name__)


def _request_endpoint_family(path: str) -> str:
    """Return a fixed diagnostic label, never caller-supplied identifiers."""
    path = path.partition("?")[0].partition("#")[0]
    for family in (
        "/Auth/Authenticate",
        "/Auth/Refresh",
        "/user/GetUserInfo",
        "/LightAndZones/OrderedList",
        "/Group/AllGroupsByLocation",
        "/Commands/On",
        "/Commands/Off",
        "/Commands/Brightness",
        "/Commands/SetColor",
    ):
        if path == family or path.startswith(f"{family}/"):
            return family
    return "other"


class _ExplicitAuthorization(requests.auth.AuthBase):
    """Keep the caller's Authorization header authoritative over ambient netrc."""

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        return request


class Credentials:
    """Own one authenticated session; share the provider budget across clients.

    All session use, token renewal, and retries hold the account gate's lock.
    Requests uses explicit connect/read inactivity timeouts, not a hard total
    deadline. Only a 401 permits one token renewal and retry; transport failures
    and commands are never silently replayed.
    """

    DEFAULT_REQUEST_INTERVAL = RequestGate.DEFAULT_REQUEST_INTERVAL
    RATE_LIMIT_BACKOFF_STEPS = RequestGate.RATE_LIMIT_BACKOFF_STEPS

    def __init__(
        self,
        min_request_interval: float | None = None,
        *,
        request_gate: RequestGate | None = None,
        api_config: ApiConfig | None = None,
    ) -> None:
        if request_gate is None:
            request_gate = RequestGate(
                self.DEFAULT_REQUEST_INTERVAL
                if min_request_interval is None else min_request_interval
            )
        elif min_request_interval is not None and (
            min_request_interval != request_gate.min_request_interval
        ):
            raise ValueError("Configure request spacing on the shared RequestGate")
        self._gate = request_gate
        self._request_lock = request_gate.lock
        self._api_config = api_config or ApiConfig()
        self._session = requests.Session()
        # Explicit request headers define Haven authentication. Environment proxy
        # and CA settings remain available, but netrc must not replace the JWT.
        self._session.auth = _ExplicitAuthorization()
        self._closed = False
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: int | str | None = None
        self._session_device_id = str(uuid.uuid4())

    @property
    def is_authenticated(self) -> bool:
        return not self._closed and bool(self._token and self._user_id)

    @property
    def account_id(self) -> str | None:
        """Return the authenticated API account ID, never the login email."""
        return str(self._user_id) if self._user_id is not None else None

    @property
    def request_gate(self) -> RequestGate:
        return self._gate

    @property
    def rate_limit_remaining(self) -> int:
        return self._gate.rate_limit_remaining

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close pooled connections without altering the account's cooldown.

        This method is synchronous and waits for any active account request.
        Home Assistant callers should invoke it through the executor.
        """
        with self._request_lock:
            if not self._closed:
                self._session.close()
                self._closed = True
                self._token = None
                self._refresh_token = None

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        """Accept delay seconds or an HTTP date without rounding down."""
        if not value:
            return None
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0, math.ceil(seconds))
            return None
        except (TypeError, ValueError):
            pass
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0, math.ceil(deadline.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            logger.debug("Unable to parse Retry-After header")
            return None

    def authenticate(self, email: str, password: str) -> bool:
        """Authenticate without a stale bearer token on the anonymous endpoint."""
        with self._request_lock:
            payload = {
                "Username": email,
                "Email": email,
                "Password": password,
                "DeviceId": self._session_device_id,
            }
            try:
                response = self._make_request_internal(
                    "POST", "/Auth/Authenticate", json=payload, auth_required=False
                )
            except ApiError as error:
                # A rejected sign-in needs attention, not repeated setup calls
                # with the same input. This status does not prove a bad password.
                # Keep transient errors and other endpoints' 400s unchanged.
                if error.code == 400:
                    raise AuthenticationError(
                        "Haven rejected the sign-in request (HTTP 400). "
                        "Check the account in the Haven app, then reauthenticate.",
                        code=400,
                    ) from error
                raise
            self._update_credentials(response, preserve_refresh_token=False)
            logger.debug("Haven authentication succeeded")
            return True

    def refresh_token(self) -> bool:
        """Refresh credentials while preserving the original rate-limit error."""
        with self._request_lock:
            if not self._refresh_token or not self._user_id:
                return False
            response = self._make_request_internal(
                "POST",
                "/Auth/Refresh",
                json={"refreshToken": self._refresh_token, "userId": self._user_id},
                auth_required=False,
            )
            self._update_credentials(response)
            return True

    def _update_credentials(
        self, data: dict[str, Any], *, preserve_refresh_token: bool = True
    ) -> None:
        """Validate all fields before replacing working credentials."""
        if not isinstance(data, dict):
            raise ApiError("Invalid authentication response: expected an object")
        token = data.get("token")
        user_id = data.get("id")
        if not isinstance(token, str) or not token.strip():
            raise ApiError("Invalid authentication response: missing token")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, (int, str))
            or (isinstance(user_id, int) and user_id <= 0)
            or (isinstance(user_id, str) and (not user_id.strip() or len(user_id) > 256))
        ):
            raise ApiError("Invalid authentication response: invalid user ID")
        if isinstance(user_id, str):
            user_id = user_id.strip()

        previous_refresh = (
            self._refresh_token
            if preserve_refresh_token and str(user_id) == self.account_id else None
        )
        refresh_token = data.get("refreshToken", previous_refresh)
        if "refreshToken" in data and (
            not isinstance(refresh_token, str) or not refresh_token.strip()
        ):
            raise ApiError("Invalid authentication response: invalid refresh token")

        self._token = token
        self._refresh_token = refresh_token
        self._user_id = user_id

    def make_request(
        self,
        method: str,
        path: str,
        auth_required: bool = True,
        use_prod_api: bool = False,
        timeout: int | float | None = None,
        *,
        allow_empty_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Make a request, with one serialized token renewal after a 401.

        Callers may opt into empty successful command acknowledgments. All
        nonempty responses still require valid JSON.
        """
        with self._request_lock:
            try:
                return self._make_request_internal(
                    method, path, auth_required, use_prod_api, timeout,
                    allow_empty_response=allow_empty_response, **kwargs
                )
            except AuthenticationError:
                if not auth_required:
                    raise
                if self.refresh_token():
                    return self._make_request_internal(
                        method, path, auth_required, use_prod_api, timeout,
                        allow_empty_response=allow_empty_response, **kwargs
                    )
                raise AuthenticationError("Token refresh failed") from None

    def _make_request_internal(
        self,
        method: str,
        path: str,
        auth_required: bool = True,
        use_prod_api: bool = False,
        timeout: int | float | None = None,
        *,
        allow_empty_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        with self._request_lock:
            if self._closed:
                raise ApiError("Haven client is closed")
            self._gate.raise_if_blocked()
            if auth_required and not self.is_authenticated:
                raise AuthenticationError("Authentication required")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                raise ApiError("Invalid Haven API request path")
            if timeout is None:
                request_timeout = self._api_config.timeout
            elif (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("timeout must be a finite positive number")
            else:
                request_timeout = (min(self._api_config.connect_timeout, timeout), timeout)

            base_url = (
                self._api_config.data_base_url if use_prod_api
                else self._api_config.auth_base_url
            )
            headers = requests.structures.CaseInsensitiveDict(
                kwargs.pop("headers", {}) or {}
            )
            if auth_required and self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            else:
                headers.pop("Authorization", None)
            headers.setdefault("User-Agent", self._api_config.user_agent)
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
            kwargs["headers"] = headers
            # Never let a redirect forward a login body or bypass our gate.
            kwargs["allow_redirects"] = False
            kwargs["stream"] = False

            self._gate.before_request()
            response = None
            try:
                # Count transport attempts, not local cooldown rejections. A
                # connection failure does not prove the request reached Haven.
                started = time.monotonic()
                try:
                    response = self._session.request(
                        method, f"{base_url}{path}", timeout=request_timeout, **kwargs
                    )
                finally:
                    log_method = method.upper() if isinstance(method, str) else "OTHER"
                    if log_method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        log_method = "OTHER"
                    logger.debug(
                        "Haven HTTP attempt: method=%s endpoint=%s status=%s elapsed=%.3fs",
                        log_method,
                        _request_endpoint_family(path),
                        response.status_code if response is not None else "request_failed",
                        time.monotonic() - started,
                    )
                status = response.status_code
                retry_after = self._parse_retry_after(response.headers.get("Retry-After"))

                if 300 <= status < 400:
                    self._gate.record_error()
                    raise ApiError("Unexpected redirect from Haven API", code=status)
                if status == 401:
                    self._gate.record_error()
                    raise AuthenticationError("Received 401 Unauthorized response")
                if status == 429:
                    cooldown = self._gate.record_rate_limit(retry_after)
                    logger.warning("Haven API rate limited; requests paused for %ss", cooldown)
                    raise ApiError(
                        "Received 429 Too Many Requests response",
                        code=429,
                        retry_after=cooldown,
                    )
                if status == 503 and retry_after is not None:
                    cooldown = self._gate.record_service_unavailable(retry_after)
                    logger.warning("Haven API unavailable; requests paused for %ss", cooldown)
                    raise ApiError(
                        "Received 503 Service Unavailable response",
                        code=503,
                        retry_after=cooldown,
                    )

                response.raise_for_status()
                if status == 204 or (
                    allow_empty_response and 200 <= status < 300 and not response.content
                ):
                    data = {}
                else:
                    data = response.json()
                self._gate.record_success()
                return data
            except requests.exceptions.JSONDecodeError as error:
                self._gate.record_error()
                status = response.status_code if response is not None else None
                detail = (
                    "an empty response"
                    if response is not None and not response.content else "invalid JSON"
                )
                raise ApiError(
                    f"Haven API returned {detail} for {_request_endpoint_family(path)} "
                    f"(HTTP {status})",
                    code=status,
                ) from error
            except requests.exceptions.RequestException as error:
                self._gate.record_error()
                status = response.status_code if response is not None else None
                raise ApiError(f"Request failed: {error}", code=status) from error
            finally:
                if response is not None:
                    response.close()
