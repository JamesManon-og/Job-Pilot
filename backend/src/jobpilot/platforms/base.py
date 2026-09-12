"""The job-board adapter contract.

One adapter per platform, responsible for everything platform-specific:
login detection, searching, extracting postings, and revealing the
application form. Everything generic — dedup, filtering, persistence,
matching, autofill, approval — lives outside adapters and is shared.

Discovery is two-phase so already-processed jobs are cheap:
    search()  yields lightweight Jobs from results pages (title, company,
              URL, external id). The runner drops ones it has seen before.
    enrich()  opens the detail page for new jobs only (description, salary).

Adapters never type credentials, never solve CAPTCHAs, and never click a
final submit button. Walls raise SessionExpiredError / ChallengeError and the
runner pauses that platform for the human.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar

from playwright.async_api import BrowserContext, Page
from pydantic import BaseModel, Field

from jobpilot.autofill.engine import detect_blocker
from jobpilot.config.preferences import PlatformSettings
from jobpilot.domain.enums import JobSource, SessionStatus
from jobpilot.domain.models import Job
from jobpilot.scrapers.http import RateLimiter


class SearchQuery(BaseModel):
    keywords: str = Field(min_length=1)
    location: str = ""
    remote_only: bool = False
    max_results: int = Field(default=25, ge=1)
    posted_within_days: int | None = None


class PlatformError(Exception):
    def __init__(self, platform: JobSource, message: str) -> None:
        super().__init__(f"{platform.value}: {message}")
        self.platform = platform


class SessionExpiredError(PlatformError):
    """Logged out / login required. The human must run `jobpilot login`."""

    def __init__(self, platform: JobSource, url: str = "") -> None:
        where = f" (redirected to {url})" if url else ""
        super().__init__(platform, f"login required{where} — run: jobpilot login {platform.value}")
        self.url = url


class ChallengeError(PlatformError):
    """CAPTCHA, checkpoint, MFA, or bot wall. JobPilot never bypasses these."""

    def __init__(self, platform: JobSource, kind: str, url: str = "") -> None:
        super().__init__(
            platform,
            f"{kind} page at {url or 'the site'} — JobPilot won't bypass it. "
            f"Open it yourself with `jobpilot login {platform.value}`, then retry.",
        )
        self.kind = kind
        self.url = url


class PlatformAdapter(ABC):
    platform: ClassVar[JobSource]
    display_name: ClassVar[str]
    home_url: ClassVar[str]
    login_url: ClassVar[str | None] = None  # None: no account involved at all
    uses_browser: ClassVar[bool] = True
    requires_login_to_search: ClassVar[bool] = False
    requires_login_to_apply: ClassVar[bool] = True
    # URL fragments that mean "you are not logged in" on this platform.
    logged_out_url_patterns: ClassVar[tuple[str, ...]] = ()
    challenge_url_patterns: ClassVar[tuple[str, ...]] = ()

    def __init__(self, settings: PlatformSettings | None = None) -> None:
        self.settings = settings or PlatformSettings(enabled=True)
        self.limiter = RateLimiter(min_interval_seconds=self.settings.min_delay_seconds)

    # -- discovery ---------------------------------------------------------------

    @abstractmethod
    def search(self, query: SearchQuery, browser: BrowserContext | None) -> AsyncIterator[Job]:
        """Yield postings for `query` from results pages (details may be missing)."""
        raise NotImplementedError

    async def enrich(self, job: Job, browser: BrowserContext | None) -> Job:
        """Fill in details (description, salary…) for a newly discovered posting."""
        return job

    # -- sessions ------------------------------------------------------------------

    async def session_status(self, browser: BrowserContext) -> SessionStatus:
        """Actively check the saved login (may navigate a fresh tab)."""
        return SessionStatus.NOT_REQUIRED if self.login_url is None else SessionStatus.UNKNOWN

    async def looks_logged_in(self, browser: BrowserContext, page: Page) -> bool:
        """Passive check while the human is logging in: never navigates."""
        return False

    # -- applying --------------------------------------------------------------------

    async def open_application(self, page: Page, job: Job) -> Page:
        """Navigate to the posting and reveal its application form.

        Returns the page holding the form (a new tab if the board hands off to
        the employer's site). Must never click a final submit button.
        """
        await self.goto(page, job.application_url)
        return page

    # -- helpers for subclasses -------------------------------------------------------

    async def goto(self, page: Page, url: str) -> None:
        """Polite navigation followed by a wall check."""
        await self.limiter.wait()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self.guard(page)

    async def guard(self, page: Page) -> None:
        """Raise if the current page is a login wall or a challenge."""
        url = page.url.lower()
        if any(p in url for p in self.challenge_url_patterns):
            raise ChallengeError(self.platform, "checkpoint", page.url)
        if any(p in url for p in self.logged_out_url_patterns):
            raise SessionExpiredError(self.platform, page.url)
        blocker = await detect_blocker(page)
        if blocker == "login":
            raise SessionExpiredError(self.platform, page.url)
        if blocker in ("challenge", "mfa"):
            raise ChallengeError(self.platform, blocker, page.url)


# Tokens like "react", "c#", "c++", "next.js" — without sentence-ending dots.
_WORD = re.compile(r"[a-z0-9+#]+(?:\.[a-z0-9]+)*")
_GENERIC_TITLE_WORDS = frozenset(
    {
        "developer",
        "engineer",
        "dev",
        "senior",
        "junior",
        "mid",
        "lead",
        "remote",
        "the",
        "and",
        "of",
        "a",
        "an",
        "job",
        "jobs",
        "role",
    }
)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace("-", " ").replace("/", " "))


def matches_keywords(query: str, *texts: str) -> bool:
    """Client-side keyword filter for boards without server-side search.

    Every distinctive word of the query must appear as a whole token ("react"
    doesn't match "reactive"); generic words like "developer" are ignored
    unless the query has nothing else. "full stack" also matches "fullstack".
    """
    words = _tokens(query)
    distinctive = [w for w in words if w not in _GENERIC_TITLE_WORDS] or words
    if not distinctive:
        return True
    tokens = _tokens(" ".join(texts))
    present = set(tokens)
    if all(w in present for w in distinctive):
        return True
    return len(distinctive) > 1 and "".join(distinctive) in present
