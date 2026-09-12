"""Pause / unpause switch for the agent (`jobpilot pause`, `jobpilot unpause`).

A flag file, so it works across processes: pausing from one terminal (or the
dashboard) makes a running search or pipeline stop at the next listing, and
prevents new runs from starting until you resume. Nothing in flight is left
half-written: every job is committed individually.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

PAUSE_FILE = "PAUSED"


class AgentPausedError(RuntimeError):
    def __init__(self, reason: str = "") -> None:
        extra = f" ({reason})" if reason else ""
        super().__init__(f"JobPilot is paused{extra}. Run `jobpilot unpause` to continue.")


def pause_path(data_dir: Path) -> Path:
    return data_dir / PAUSE_FILE


def is_paused(data_dir: Path) -> bool:
    return pause_path(data_dir).exists()


def pause_reason(data_dir: Path) -> str:
    try:
        return pause_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pause(data_dir: Path, reason: str = "") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    pause_path(data_dir).write_text(f"{stamp} {reason}".strip() + "\n", encoding="utf-8")


def resume(data_dir: Path) -> bool:
    try:
        pause_path(data_dir).unlink()
    except FileNotFoundError:
        return False
    return True


def ensure_running(data_dir: Path) -> None:
    if is_paused(data_dir):
        raise AgentPausedError(pause_reason(data_dir))
