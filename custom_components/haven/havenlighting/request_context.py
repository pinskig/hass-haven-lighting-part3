"""Bounded, process-local correlation for one user action's HTTP attempts."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import re
import uuid


_REQUEST_TRACE: ContextVar[str] = ContextVar("haven_request_trace", default="-")
_TRACE_FORMAT = re.compile(r"[0-9a-f]{12}\Z", flags=re.ASCII)


def new_request_trace() -> str:
    """Generate an action identifier without account or device information."""
    return uuid.uuid4().hex[:12]


def get_request_trace() -> str:
    """Return the current action identifier, or '-' outside an action."""
    return _REQUEST_TRACE.get()


@contextmanager
def request_trace(trace_id: str) -> Iterator[None]:
    """Scope a code-generated identifier; executor callers must copy context."""
    if not isinstance(trace_id, str) or _TRACE_FORMAT.fullmatch(trace_id) is None:
        raise ValueError("Request trace must contain 12 lowercase hexadecimal characters")
    token = _REQUEST_TRACE.set(trace_id)
    try:
        yield
    finally:
        _REQUEST_TRACE.reset(token)
