"""Cross-process locks so two pipeline runs can't race each other.

Two `jobpilot run`s (or a CLI run plus the dashboard's "Scrape now") would
score the same jobs twice and fight over inserts. An flock()ed file serializes
them. The OS drops the lock when the holder exits or crashes, so a killed
process never leaves a stale lock behind.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PIPELINE_LOCK = "pipeline"


class LockBusyError(RuntimeError):
    def __init__(self, name: str, path: Path, holder: str) -> None:
        who = f" (pid {holder})" if holder else ""
        super().__init__(
            f"Another JobPilot {name} run is already in progress{who}. "
            f"Wait for it to finish, or stop it first. Lock file: {path}"
        )
        self.name = name
        self.path = path


@contextmanager
def process_lock(name: str, lock_dir: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock named `name` for the block's duration."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode("ascii", "ignore").strip()
            raise LockBusyError(name, path, holder) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
