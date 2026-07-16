"""Structured logging: rich console output + rotating file log."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Idempotent: calling twice (tests, CLI + API in one process) must not duplicate handlers.
    root.handlers.clear()

    console = RichHandler(rich_tracebacks=True, show_path=False)
    console.setLevel(level.upper())
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)

    # Keep SQLAlchemy quiet unless explicitly debugging.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
