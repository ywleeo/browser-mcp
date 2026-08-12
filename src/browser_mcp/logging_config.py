"""Logging configuration that preserves stdout for MCP framing."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int) -> None:
    """Configure root logging on stderr without ever touching stdout."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
