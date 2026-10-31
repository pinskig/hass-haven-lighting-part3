import logging
from typing import Dict, Any, Optional
from .credentials import Credentials
from .devices.light import Light
from .devices.location import Location
from .exceptions import AuthenticationError, ApiError
from .logging import setup_logging

logger = logging.getLogger(__name__)

class HavenClient:
    """Main client for interacting with Haven Lighting devices."""
    
    def __init__(self, log_level: int = logging.INFO, log_file: Optional[str] = None) -> None:
        """
        Initialize the Haven Lighting client.
        
        Args:
            log_level: Logging level (default: INFO)
            log_file: Optional file path for logging output
        """
        setup_logging(log_level, log_file)
        self._credentials = Credentials()
        self._locations: Dict[int, Location] = {}
        self._lights: Dict[int, Light] = {}
        logger.debug("Initialized HavenClient")

    @property
    def is_authenticated(self) -> bool:
        """Whether this client already holds credentials for setup retries."""
        return self._credentials.is_authenticated

    def authenticate(self, email: str, password: str) -> bool:
        """
        Authenticate with the Haven Lighting service.
        
        Args:
            email: User's email address
            password: User's password
            
        Returns:
            bool: True if authentication successful, False otherwise
            
        Raises:
            ApiError: If API request fails
        """
        try:
            authenticated = self._credentials.authenticate(email, password)
            if authenticated:
                logger.info("Successfully authenticated user: %s", email)
            else:
                logger.warning("Authentication failed for user: %s", email)
            return authenticated
        except ApiError as e:
            logger.error("API error during authentication: %s", str(e))
            raise

    def discover_locations(self) -> Dict[int, Location]:
        """Discover all available locations."""
        if not self._credentials:
            raise AuthenticationError("Not authenticated")
            
        locations = Location.discover(self._credentials)
        self._locations.update(locations)
        return self._locations 
