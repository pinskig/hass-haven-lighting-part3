from typing import Dict, Any, Optional
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import requests
import logging
import threading
import time
import uuid
from .exceptions import AuthenticationError, ApiError
from .config import API_TIMEOUT

# GIADA FIX: Pointing to STAGING (stg-api) because that is where the account lives
AUTH_API_BASE = "https://stg-api.havenlighting.com/api"
PROD_API_BASE = "https://stg-api.havenlighting.com/api"

logger = logging.getLogger(__name__)

class Credentials:
    """Own authentication and the shared request budget for one client.

    Every endpoint, including login, token renewal, and commands, uses the same
    transport lock and cooldown. This synchronous client runs in HA's executor;
    only the short spacing between requests sleeps. Longer cooldowns raise an
    ApiError immediately so the caller can schedule a later attempt.
    """

    DEFAULT_REQUEST_INTERVAL = 1.0
    RATE_LIMIT_BACKOFF_STEPS = (60, 300, 900)
    STABLE_SUCCESS_THRESHOLD = 3
    
    def __init__(self, min_request_interval: float = DEFAULT_REQUEST_INTERVAL):
        if not math.isfinite(min_request_interval) or min_request_interval < 0:
            raise ValueError("min_request_interval must be a finite, nonnegative number")
        self._request_lock = threading.RLock()
        self._min_request_interval = float(min_request_interval)
        self._next_request_at = 0.0
        self._rate_limited_until = 0.0
        self._consecutive_429 = 0
        self._consecutive_successes = 0
        self._token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._user_id: Optional[int] = None
        # Generate a standard UUID for the session
        self._session_device_id = str(uuid.uuid4())
        logger.debug(f"Initialized Credentials with Device ID: {self._session_device_id}")
        
    @property
    def is_authenticated(self) -> bool:
        return bool(self._token and self._user_id)

    @property
    def rate_limit_remaining(self) -> int:
        """Return the remaining shared cooldown, rounded up to whole seconds."""
        with self._request_lock:
            return max(0, math.ceil(self._rate_limited_until - time.monotonic()))

    def _raise_if_rate_limited(self) -> None:
        """Block locally without extending an existing server cooldown."""
        remaining = self.rate_limit_remaining
        if remaining:
            raise ApiError(
                f"Haven API cooldown active; retry in {remaining} seconds",
                code=429,
                retry_after=remaining,
                rate_limited_locally=True,
            )

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[int]:
        """Accept delay seconds or an HTTP date without rounding a delay down."""
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
            logger.debug("Unable to parse Retry-After header value: %s", value)
            return None

    def _record_rate_limit(self, retry_after: Optional[int]) -> int:
        """Start/extend cooldown only after an actual HTTP 429 response."""
        self._consecutive_429 += 1
        self._consecutive_successes = 0
        step = min(self._consecutive_429 - 1, len(self.RATE_LIMIT_BACKOFF_STEPS) - 1)
        delay = max(self.RATE_LIMIT_BACKOFF_STEPS[step], retry_after or 0)
        self._rate_limited_until = max(
            self._rate_limited_until, time.monotonic() + delay
        )
        logger.warning(
            "Haven API rate limited; all requests paused for %ss (Retry-After=%s)",
            self.rate_limit_remaining,
            retry_after,
        )
        return self.rate_limit_remaining

    def _record_success(self) -> None:
        self._consecutive_successes += 1
        if self._consecutive_successes >= self.STABLE_SUCCESS_THRESHOLD:
            self._consecutive_429 = 0
        
    def authenticate(self, email: str, password: str) -> bool:
        """Authenticate with the Haven Lighting service."""
        with self._request_lock:
            return self._authenticate_locked(email, password)

    def _authenticate_locked(self, email: str, password: str) -> bool:
        logger.debug("Attempting authentication for user: %s", email)
        
        # GIADA FIX: The "Double-Barreled" Payload
        # We send BOTH Username and Email to satisfy different server versions.
        # We point to stg-api because that is where the account validated.
        payload = {
            "Username": email,  # Required by Staging
            "Email": email,     # Required by newer logic
            "Password": password,
            "DeviceId": self._session_device_id
        }
        
        try:
            # Headers to mimic a real browser/app
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            response = self._make_request_internal(
                "POST",
                "/Auth/Authenticate", 
                json=payload,
                headers=headers,
                auth_required=False
            )
            
            self._update_credentials(response)
            logger.info("Successfully authenticated user: %s", email)
            return True
            
        except ApiError as e:
            logger.error("Authentication failed for user %s: %s", email, str(e))
            raise
            
    def refresh_token(self) -> bool:
        """Refresh the authentication token."""
        with self._request_lock:
            return self._refresh_token_locked()

    def _refresh_token_locked(self) -> bool:
        if not self._refresh_token or not self._user_id:
            logger.debug("Cannot refresh token - missing refresh token or user ID")
            return False
        
        try:
            logger.debug("Attempting token refresh for user ID: %s", self._user_id)
            response = self._make_request_internal(
                "POST",
                "/Auth/Refresh",
                json={
                    "refreshToken": self._refresh_token,
                    "userId": self._user_id
                },
                auth_required=False
            )
            self._update_credentials(response)
            logger.debug("Token refresh successful")
            return True
            
        except ApiError as e:
            logger.error("Token refresh failed: %s", str(e))
            raise
            
    def _update_credentials(self, data: Dict[str, Any]) -> None:
        """Validate the complete response before replacing working credentials."""
        if not isinstance(data, dict):
            raise ApiError("Invalid authentication response: expected an object")
        token = data.get("token")
        user_id = data.get("id")
        if not isinstance(token, str) or not token.strip() or not user_id:
            raise ApiError("Invalid authentication response: missing token or user ID")

        # Some renewals may keep the existing refresh token. An omitted field
        # must not erase it; an explicitly malformed field rejects the response.
        refresh_token = data.get("refreshToken", self._refresh_token)
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
        timeout: int = API_TIMEOUT,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """Make an authenticated API request with automatic token refresh."""
        # Hold the lock through token renewal and the retry. A queued caller
        # therefore starts with the renewed token rather than refreshing again.
        with self._request_lock:
            return self._make_request_locked(
                method, path, auth_required, use_prod_api, timeout, **kwargs
            )

    def _make_request_locked(
        self,
        method: str,
        path: str,
        auth_required: bool,
        use_prod_api: bool,
        timeout: int,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            return self._make_request_internal(
                method=method, 
                path=path, 
                auth_required=auth_required,
                use_prod_api=use_prod_api,
                timeout=timeout,
                **kwargs
            )
        except AuthenticationError:
            if not auth_required:
                raise
            logger.info("Authentication error, attempting token refresh")
            if self.refresh_token():
                logger.info("Token refresh successful, retrying request")
                return self._make_request_internal(
                    method=method, 
                    path=path, 
                    auth_required=auth_required,
                    use_prod_api=use_prod_api,
                    timeout=timeout,
                    **kwargs
                )
            logger.error("Token refresh failed, unable to retry request")
            raise AuthenticationError("Token refresh failed")
        
    def _make_request_internal(
        self, 
        method: str, 
        path: str, 
        auth_required: bool = True,
        use_prod_api: bool = False,
        timeout: int = API_TIMEOUT,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """Internal method for making API requests."""
        with self._request_lock:
            return self._request_locked(
                method, path, auth_required, use_prod_api, timeout, **kwargs
            )

    def _request_locked(
        self,
        method: str,
        path: str,
        auth_required: bool,
        use_prod_api: bool,
        timeout: int,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self._raise_if_rate_limited()
        if auth_required and not self.is_authenticated:
            raise AuthenticationError("Authentication required")

        # This is the only sleep: a short gap between outbound requests. Long
        # server cooldowns above return an error immediately instead of sleeping.
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._raise_if_rate_limited()
        self._next_request_at = time.monotonic() + self._min_request_interval
            
        base_url = PROD_API_BASE if use_prod_api else AUTH_API_BASE
        url = f"{base_url}{path}"
        
        # Manage Headers
        headers = dict(kwargs.pop("headers", {}) or {})
        
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        
        if "User-Agent" not in headers:
             headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        
        kwargs["headers"] = headers
            
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)

            rate_limit = response.headers.get("X-RateLimit-Limit")
            rate_remaining = response.headers.get("X-RateLimit-Remaining")
            retry_after = response.headers.get("Retry-After")
            if rate_limit or rate_remaining or retry_after:
                logger.debug(
                    "Rate-limit headers for %s %s -> limit=%s remaining=%s retry_after=%s",
                    method,
                    path,
                    rate_limit,
                    rate_remaining,
                    retry_after,
                )
            
            if response.status_code == 401:
                self._consecutive_successes = 0
                raise AuthenticationError("Received 401 Unauthorized response")

            if response.status_code == 429:
                retry_after_seconds = self._parse_retry_after(retry_after)
                cooldown = self._record_rate_limit(retry_after_seconds)
                raise ApiError(
                    "Received 429 Too Many Requests response",
                    code=429,
                    retry_after=cooldown,
                )
            
            response.raise_for_status()
            
            if response.status_code == 204:
                self._record_success()
                return {}
                
            data = response.json()
            self._record_success()
            return data
            
        except requests.exceptions.RequestException as e:
            self._consecutive_successes = 0
            logger.error("Request failed: %s", str(e))
            status = response.status_code if 'response' in locals() else None
            raise ApiError(f"Request failed: {str(e)}", code=status) from e
