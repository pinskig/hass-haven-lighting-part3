"""Optional explicit logging setup for standalone applications.

Home Assistant owns its own logging configuration. Client construction never
calls this helper or adds handlers.
"""

import logging
from typing import Optional


def setup_logging(
    level: int = logging.INFO, log_file: Optional[str] = None
) -> None:
    """Configure this package once, without accumulating owned handlers."""
    logger = logging.getLogger(__package__)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_haven_owned_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        file_handler._haven_owned_handler = True
        logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler._haven_owned_handler = True
    logger.addHandler(console_handler)
