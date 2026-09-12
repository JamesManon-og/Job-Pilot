"""RemoteOK — public JSON API, no account, no browser.

The API returns every recent posting at once, so it's fetched once per run
and filtered by each query's keywords client-side. Applying happens on the
employer's site, which the generic autofill handles.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from playwright.async_api import BrowserContext

from jobpilot.config.preferences import PlatformSettings
from jobpilot.domain.enums import JobSource
from jobpilot.domain.models import Job
from jobpilot.platforms.base import PlatformAdapter, SearchQuery, matches_keywords
from jobpilot.platforms.registry import register_platform
from jobpilot.scrapers.remoteok import RemoteOKScraper


@register_platform
class RemoteOKAdapter(PlatformAdapter):
    platform: ClassVar[JobSource] = JobSource.REMOTEOK
    display_name: ClassVar[str] = "RemoteOK"
    home_url: ClassVar[str] = "https://remoteok.com"
    uses_browser: ClassVar[bool] = False
    requires_login_to_apply: ClassVar[bool] = False

    def __init__(
        self, settings: PlatformSettings | None = None, *, scraper: RemoteOKScraper | None = None
    ) -> None:
        super().__init__(settings)
        self._scraper = scraper or RemoteOKScraper()
        self._listings: list[Job] | None = None

    async def search(
        self, query: SearchQuery, browser: BrowserContext | None
    ) -> AsyncIterator[Job]:
        if self._listings is None:  # one API call per run, whatever the query count
            self._listings = [job async for job in self._scraper.scrape()]
        yielded = 0
        for job in self._listings:
            if not matches_keywords(query.keywords, job.title, " ".join(job.technologies)):
                continue
            yield job
            yielded += 1
            if yielded >= query.max_results:
                return
