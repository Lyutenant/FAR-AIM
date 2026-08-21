"""Logging setup shared by the CLI and future scheduled jobs."""

from __future__ import annotations

import logging


def setup_logging(verbosity: int = 0) -> None:
    if verbosity <= 0:
        level = logging.WARNING
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
