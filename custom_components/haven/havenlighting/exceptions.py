from typing import Optional

class HavenException(Exception):
    """Base exception for Haven Lighting API errors."""
    
    def __init__(self, message: str, code: Optional[int] = None) -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)

class ApiError(HavenException):
    """Raised when an API request fails."""
    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after

class AuthenticationError(HavenException):
    """Raised when authentication fails or is required but missing."""
    pass

class DeviceError(HavenException):
    """Raised when a device operation fails."""
    pass
