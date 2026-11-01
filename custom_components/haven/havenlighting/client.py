"""A Haven API client with explicit transport ownership."""

import logging
from typing import Dict, Optional

from .config import ApiConfig
from .credentials import Credentials
from .devices.location import Location
from .exceptions import AuthenticationError
from .request_gate import RequestGate

logger = logging.getLogger(__name__)


class HavenClient:
    """Expose account discovery while retaining a shared provider request gate."""

    def __init__(
        self,
        log_level: int = logging.INFO,
        log_file: Optional[str] = None,
        *,
        request_gate: RequestGate | None = None,
        api_config: ApiConfig | None = None,
    ) -> None:
        """Create a client without changing the host application's logging.

        Legacy log_level/log_file arguments are retained for compatibility.
        Standalone applications can call logging.setup_logging explicitly.
        """
        self._credentials = Credentials(request_gate=request_gate, api_config=api_config)
        self._locations: Dict[int, Location] = {}

    @property
    def is_authenticated(self) -> bool:
        return self._credentials.is_authenticated

    @property
    def account_id(self) -> str | None:
        """Return Haven's stable account ID for entity/device namespacing."""
        return self._credentials.account_id

    @property
    def request_gate(self) -> RequestGate:
        return self._credentials.request_gate

    @property
    def closed(self) -> bool:
        return self._credentials.closed

    def authenticate(self, email: str, password: str) -> bool:
        return self._credentials.authenticate(email, password)

    def discover_locations(self) -> Dict[int, Location]:
        if not self.is_authenticated:
            raise AuthenticationError("Not authenticated")
        discovered = Location.discover(self._credentials)
        self._locations = {
            location_id: self._locations.get(location_id, location)
            for location_id, location in discovered.items()
        }
        return self._locations.copy()

    def close(self) -> None:
        """Close this client's session, retaining the independently owned gate."""
        self._credentials.close()
