"""Authoritative API settings for the Haven client."""

from dataclasses import dataclass
import math
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ApiConfig:
    """Endpoints and socket inactivity limits, not a wall-clock deadline.

    Both existing endpoints intentionally remain on staging, where this account
    was verified. Do not silently switch accounts to another Haven environment.
    """

    auth_base_url: str = "https://stg-api.havenlighting.com/api"
    data_base_url: str = "https://stg-api.havenlighting.com/api"
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __post_init__(self) -> None:
        for name in ("auth_base_url", "data_base_url"):
            value = getattr(self, name)
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{name} must be an HTTPS API base URL")
            object.__setattr__(self, name, value.rstrip("/"))
        for name in ("connect_timeout", "read_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")

    @property
    def timeout(self) -> tuple[float, float]:
        """Return Requests' connect/read tuple; neither is a total deadline."""
        return (self.connect_timeout, self.read_timeout)
