from typing import Dict, Any, Optional
import requests
import logging
import uuid
from .exceptions import AuthenticationError, ApiError
from .config import DEVICE_ID, API_TIMEOUT

# GIADA FIX: Pointing to STAGING (stg-api) because that is where the account lives
AUTH_API_BASE = "https://stg-api.havenlighting.com/api"
PROD_API_BASE = "https://stg-api.havenlighting.com/api"

logger = logging.getLogger(__name__)

class Credentials:
    """Handles authentication and request credentials."""
    
    def __init__(self):
        self._token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._user_id: Optional[int] = None
        # Generate a standard UUID for the session
        self._session_device_id = str(uuid.uuid4())
        logger.debug(f"Initialized Credentials with Device ID: {self._session_device_id}")
        
    @property
    def is_authenticated(self) -> bool:
        return bool(self._token and self._user_id)
        
    def authenticate(self, email: str, password: str) -> bool:
        """Authenticate with the Haven Lighting service."""
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
            
            if not response or "token" not in response:
                logger.error("Authentication failed: No token returned for user %s", email)
                return False
                
            self._update_credentials(response)
            logger.info("Successfully authenticated user: %s", email)
            return True
            
        except ApiError as e:
            logger.error("Authentication failed for user %s: %s", email, str(e))
            return False
            
    def refresh_token(self) -> bool:
        """Refresh the authentication token."""
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
            return False
            
    def _update_credentials(self, data: Dict[str, Any]) -> None:
        """Update stored credentials from API response."""
        self._token = data.get("token")
        self._refresh_token = data.get("refreshToken")
        self._user_id = data.get("id")
        
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
        if auth_required and not self.is_authenticated:
            raise AuthenticationError("Authentication required")
            
        base_url = PROD_API_BASE if use_prod_api else AUTH_API_BASE
        url = f"{base_url}{path}"
        
        # Manage Headers
        headers = kwargs.pop("headers", {})
        
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
                raise AuthenticationError("Received 401 Unauthorized response")

            if response.status_code == 429:
                retry_after_seconds: Optional[int] = None
                retry_after_header = response.headers.get("Retry-After")
                if retry_after_header is not None:
                    try:
                        retry_after_seconds = int(retry_after_header)
                    except ValueError:
                        retry_after_seconds = None
                raise ApiError(
                    "Received 429 Too Many Requests response",
                    code=429,
                    retry_after=retry_after_seconds,
                )
            
            response.raise_for_status()
            
            if response.status_code == 204:
                return {}
                
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error("Request failed: %s", str(e))
            if 'response' in locals() and response is not None:
                logger.error("API Response Body: %s", response.text)
            raise ApiError(f"Request failed: {str(e)}")
