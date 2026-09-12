"""Persistent, per-platform browser profiles.

You log in to each board once, by hand, in a window JobPilot opens
(`jobpilot login linkedin`). Chromium keeps the cookies in
data/browser-profiles/<platform>/, and later searches and applications reuse
them. JobPilot never sees or stores a password.

One profile per platform: a LinkedIn problem can't touch your JobStreet
session, and no site's cookies leak to another. A profile can only be open
in one process at a time, so each is guarded by a lock that gives a clear
error instead of Chromium's cryptic one.

No stealth plugins, fingerprint spoofing, or user-agent tricks: it's a normal
Chromium (or your installed Chrome via `browser_channel`).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

from jobpilot.domain.enums import JobSource
from jobpilot.locks import LockBusyError, process_lock

# Chromium discards session cookies (no expiry) on exit, which would log you
# out of boards that use them after every run. JobPilot saves the context's
# storage state here (owner-only) and restores missing session cookies on open.
STATE_FILE = "jobpilot-session-state.json"


async def _save_state(context: BrowserContext, state_path: Path) -> None:
    state = await context.storage_state()
    tmp = state_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, state_path)


async def _restore_session_cookies(context: BrowserContext, state_path: Path) -> None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    existing = {(c["name"], c["domain"], c["path"]) for c in await context.cookies()}
    missing = [
        cookie
        for cookie in state.get("cookies", [])
        if cookie.get("expires", -1) in (-1, None)
        and (cookie.get("name"), cookie.get("domain"), cookie.get("path")) not in existing
    ]
    if missing:
        await context.add_cookies(missing)


class ProfileInUseError(RuntimeError):
    def __init__(self, platform: JobSource) -> None:
        super().__init__(
            f"The {platform.value} browser profile is already open in another JobPilot "
            f"process (a `jobpilot login`, `search`, or `apply` window). Close it first."
        )
        self.platform = platform


class BrowserProfiles:
    def __init__(
        self,
        profiles_dir: Path,
        locks_dir: Path,
        *,
        headless: bool = True,
        channel: str | None = None,
    ) -> None:
        self._dir = profiles_dir
        self._locks = locks_dir
        self._headless = headless
        self._channel = channel

    def profile_path(self, platform: JobSource) -> Path:
        return self._dir / platform.value

    def has_profile(self, platform: JobSource) -> bool:
        return self.profile_path(platform).is_dir()

    def _ensure_private_dir(self, path: Path) -> None:
        # Session cookies are as good as a password: owner-only permissions.
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        path.mkdir(exist_ok=True)
        os.chmod(path, 0o700)

    @asynccontextmanager
    async def open(
        self, platform: JobSource, *, headless: bool | None = None
    ) -> AsyncIterator[BrowserContext]:
        """The platform's persistent browser context, exclusively."""
        path = self.profile_path(platform)
        self._ensure_private_dir(path)
        visible = not (self._headless if headless is None else headless)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(process_lock(f"browser-{platform.value}", self._locks))
            except LockBusyError:
                raise ProfileInUseError(platform) from None
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    str(path),
                    headless=not visible,
                    channel=self._channel,
                    locale="en-US",
                    no_viewport=visible,
                )
                try:
                    await _restore_session_cookies(context, path / STATE_FILE)
                    yield context
                finally:
                    with contextlib.suppress(Exception):  # a crashed browser has no state
                        await _save_state(context, path / STATE_FILE)
                    await context.close()

    def delete_profile(self, platform: JobSource) -> bool:
        """Forget a platform's saved login (used by `jobpilot logout`)."""
        path = self.profile_path(platform)
        if not path.exists():
            return False
        with process_lock(f"browser-{platform.value}", self._locks):
            shutil.rmtree(path)
        return True
