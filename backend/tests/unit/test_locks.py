"""Cross-process pipeline lock (audit: two `jobpilot run`s raced each other)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobpilot.locks import LockBusyError, process_lock


def test_second_holder_is_refused_with_the_pid(tmp_path: Path) -> None:
    with process_lock("pipeline", tmp_path):
        with pytest.raises(LockBusyError) as excinfo, process_lock("pipeline", tmp_path):
            pass
        assert "already in progress" in str(excinfo.value)
        assert "pid" in str(excinfo.value)


def test_lock_is_released_after_the_block(tmp_path: Path) -> None:
    with process_lock("pipeline", tmp_path):
        pass
    with process_lock("pipeline", tmp_path):
        pass


def test_different_names_do_not_conflict(tmp_path: Path) -> None:
    with process_lock("pipeline", tmp_path), process_lock("apply", tmp_path):
        pass


def test_lock_held_by_another_process_and_freed_when_it_dies(tmp_path: Path) -> None:
    """A killed `jobpilot run` must not leave a stale lock behind."""
    script = (
        "import sys, time; sys.path.insert(0, 'src');"
        "from pathlib import Path; from jobpilot.locks import process_lock;"
        f"cm = process_lock('pipeline', Path({str(tmp_path)!r})); cm.__enter__();"
        "print('locked', flush=True); time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusyError), process_lock("pipeline", tmp_path):
            pass
    finally:
        child.kill()
        child.wait()
    time.sleep(0.1)
    with process_lock("pipeline", tmp_path):
        pass
