"""LinkedIn Jobs — search and Easy Apply in your own logged-in browser.

⚠️  LinkedIn's User Agreement restricts automated access. This adapter drives
*your* browser session at human pace for *your* job search; it does not create
accounts, bypass the authwall, solve challenges, or collect data beyond the
postings you searched for. Automation can still get an account restricted —
enabling this platform is your call. Keep the delays and daily caps low.

Search requires being logged in (logged-out requests land on /authwall), so
`jobpilot login linkedin` must be run first. The DOM changes often, so every
field has several selectors and a missing results list is reported as a
layout problem rather than "no jobs found".
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from jobpilot.domain.enums import EmploymentType, JobSource, RemoteType, SessionStatus
from jobpilot.domain.models import Job
from jobpilot.platforms.base import (
    ChallengeError,
    PlatformAdapter,
    PlatformError,
    SearchQuery,
    SessionExpiredError,
)
from jobpilot.platforms.parsing import (
    TEXT_OF_JS,
    as_dicts,
    clean,
    parse_relative_date,
    parse_timestamp,
)
from jobpilot.platforms.registry import register_platform

logger = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
PAGE_SIZE = 25
_JOB_ID = re.compile(r"/jobs/view/(?:[^/]*-)?(\d+)|currentJobId=(\d+)|jobPosting:(\d+)")

_CARDS_JS = """
() => {
  const cards = document.querySelectorAll(
    'div.job-card-container[data-job-id], li[data-occludable-job-id], ' +
    'li.jobs-search-results__list-item, div.base-card[data-entity-urn], ' +
    'li.scaffold-layout__list-item, .job-search-card'
  );
  const text = (el, sels) => {
    for (const sel of sels) {
      const n = el.querySelector(sel);
      const t = n ? (n.innerText || n.getAttribute('aria-label') || '') : '';
      if (t.replace(/\\s+/g, ' ').trim()) return t.replace(/\\s+/g, ' ').trim();
    }
    return '';
  };
  return Array.from(cards).map((card) => {
    const link = card.querySelector('a[href*="/jobs/view/"]');
    const time = card.querySelector('time[datetime]');
    return {
      id: card.getAttribute('data-job-id') || card.getAttribute('data-occludable-job-id') ||
          card.getAttribute('data-entity-urn') || '',
      url: link ? link.href : '',
      title: text(card, ['.job-card-list__title--link', '.job-card-list__title',
                         '.base-search-card__title', '.job-card-container__link',
                         'a[href*="/jobs/view/"] strong', 'h3']),
      company: text(card, ['.job-card-container__primary-description',
                           '.artdeco-entity-lockup__subtitle',
                           '.base-search-card__subtitle', 'h4']),
      location: text(card, ['.job-card-container__metadata-item',
                            '.artdeco-entity-lockup__caption',
                            '.job-search-card__location']),
      footer: text(card, ['.job-card-container__footer-item', '.job-search-card__listdate',
                          '.job-card-list__footer-wrapper']),
      posted: time ? time.getAttribute('datetime') : '',
    };
  });
}
"""


def job_id_from(*values: str) -> str | None:
    for value in values:
        match = _JOB_ID.search(value or "")
        if match:
            return next(group for group in match.groups() if group)
    return None


def workplace_type(*texts: str) -> RemoteType:
    blob = " ".join(texts).lower()
    if "hybrid" in blob:
        return RemoteType.HYBRID
    if "remote" in blob:
        return RemoteType.REMOTE
    if "on-site" in blob or "onsite" in blob:
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


_EMPLOYMENT = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
}


def employment_type(*texts: str) -> EmploymentType:
    blob = " ".join(texts).lower()
    for needle, value in _EMPLOYMENT.items():
        if needle in blob:
            return value
    return EmploymentType.UNKNOWN


@register_platform
class LinkedInAdapter(PlatformAdapter):
    platform: ClassVar[JobSource] = JobSource.LINKEDIN
    display_name: ClassVar[str] = "LinkedIn Jobs"
    home_url: ClassVar[str] = BASE
    login_url: ClassVar[str] = f"{BASE}/login"
    requires_login_to_search: ClassVar[bool] = True
    requires_login_to_apply: ClassVar[bool] = True
    logged_out_url_patterns: ClassVar[tuple[str, ...]] = (
        "/authwall",
        "/login",
        "/uas/login",
        "/signup",
    )
    challenge_url_patterns: ClassVar[tuple[str, ...]] = ("/checkpoint/", "/security/")

    def search_url(self, query: SearchQuery, start: int) -> str:
        params = [f"keywords={quote_plus(query.keywords)}"]
        if query.location:
            params.append(f"location={quote_plus(query.location)}")
        if query.remote_only:
            params.append("f_WT=2")  # LinkedIn's "Remote" workplace filter
        if query.posted_within_days:
            params.append(f"f_TPR=r{query.posted_within_days * 86400}")
        if start:
            params.append(f"start={start}")
        return f"{BASE}/jobs/search/?{'&'.join(params)}"

    def job_url(self, job_id: str) -> str:
        return f"{BASE}/jobs/view/{job_id}"

    async def search(
        self, query: SearchQuery, browser: BrowserContext | None
    ) -> AsyncIterator[Job]:
        if browser is None:
            raise PlatformError(self.platform, "needs a browser context")
        page = await browser.new_page()
        try:
            seen = 0
            for start in range(0, PAGE_SIZE * 6, PAGE_SIZE):
                await self.goto(page, self.search_url(query, start))
                await self._settle(page)
                cards = as_dicts(await page.evaluate(_CARDS_JS))
                if not cards:
                    if start == 0:
                        raise PlatformError(
                            self.platform,
                            "no job cards found — LinkedIn's results layout may have "
                            "changed, or the search returned nothing. Check with "
                            "`jobpilot search --platform linkedin --headed`.",
                        )
                    return
                for card in cards:
                    job = self._to_job(card)
                    if job is None:
                        continue
                    yield job
                    seen += 1
                    if seen >= query.max_results:
                        return
        finally:
            await page.close()

    async def _settle(self, page: Page) -> None:
        """LinkedIn renders results lazily; give the list a moment, then nudge it."""
        try:
            await page.wait_for_selector(
                "div.job-card-container, li[data-occludable-job-id], div.base-card, "
                ".jobs-search-results-list, .jobs-search__results-list",
                timeout=15_000,
            )
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(800)
        except PlaywrightError:
            pass  # the caller reports "no cards" with guidance

    def _to_job(self, card: dict[str, Any]) -> Job | None:
        title = clean(card.get("title"))
        job_id = job_id_from(clean(card.get("id")), clean(card.get("url")))
        if not title or not job_id:
            return None
        location = clean(card.get("location"))
        footer = clean(card.get("footer"))
        posted = clean(card.get("posted"))
        return Job(
            title=title,
            company=clean(card.get("company")) or "Unknown company",
            location=location or None,
            remote=workplace_type(location, title, footer),
            application_url=self.job_url(job_id),
            external_id=job_id,
            source=self.platform,
            date_posted=parse_timestamp(posted) or parse_relative_date(footer),
        )

    async def enrich(self, job: Job, browser: BrowserContext | None) -> Job:
        if browser is None:
            return job
        page = await browser.new_page()
        try:
            await self.goto(page, job.application_url)
            description = await self._text(
                page,
                [
                    ".jobs-description__content",
                    ".jobs-box__html-content",
                    "#job-details",
                    ".show-more-less-html__markup",
                    ".description__text",
                ],
            )
            insights = await self._text(
                page,
                [
                    ".job-details-jobs-unified-top-card__job-insight",
                    ".jobs-unified-top-card__job-insight",
                    ".description__job-criteria-list",
                    ".job-details-preferences-and-skills",
                ],
            )
            company = await self._text(
                page,
                [
                    ".job-details-jobs-unified-top-card__company-name",
                    ".jobs-unified-top-card__company-name",
                    ".topcard__org-name-link",
                ],
            )
            return job.model_copy(
                update={
                    "description": description or job.description,
                    "company": company or job.company,
                    "employment_type": employment_type(insights) or job.employment_type,
                    "remote": workplace_type(insights, job.location or "", job.title)
                    if insights
                    else job.remote,
                }
            ).reidentified()
        finally:
            await page.close()

    async def _text(self, page: Page, selectors: list[str]) -> str:
        value = await page.evaluate(TEXT_OF_JS, {"selectors": selectors, "root": None})
        return clean(value if isinstance(value, str) else "")

    async def session_status(self, browser: BrowserContext) -> SessionStatus:
        page = await browser.new_page()
        try:
            await self.goto(page, f"{BASE}/jobs/")
        except SessionExpiredError:
            return SessionStatus.NEEDS_LOGIN
        except ChallengeError:
            return SessionStatus.BLOCKED
        else:
            return (
                SessionStatus.LOGGED_IN
                if await self.looks_logged_in(browser, page)
                else SessionStatus.NEEDS_LOGIN
            )
        finally:
            await page.close()

    async def looks_logged_in(self, browser: BrowserContext, page: Page) -> bool:
        # li_at is LinkedIn's auth cookie; JobPilot only ever checks that it exists.
        cookies = {c["name"] for c in await browser.cookies()}
        return "li_at" in cookies and not any(
            p in page.url.lower() for p in (*self.logged_out_url_patterns, "/checkpoint/")
        )

    async def open_application(self, page: Page, job: Job) -> Page:
        """Open the posting and click Easy Apply (or the employer's Apply link).

        Only the button that *opens* the form is clicked. Steps ("Next",
        "Review") and "Submit application" are always left to the human.
        """
        await self.goto(page, job.application_url)
        known = set(page.context.pages)
        # Both Easy Apply and the employer hand-off use .jobs-apply-button; only
        # the label tells them apart, so match on the label first.
        easy = page.locator(
            "button[aria-label*='Easy Apply' i], a[aria-label*='Easy Apply' i], "
            "button:has-text('Easy Apply')"
        ).first
        any_apply = page.locator(
            "button.jobs-apply-button, button[aria-label^='Apply' i], "
            "a[aria-label^='Apply' i], button:has-text('Apply'), a:has-text('Apply')"
        ).first
        button = easy if await easy.count() else any_apply
        if not await button.count():
            raise PlatformError(
                self.platform,
                "no Apply button on this posting (it may be closed or already applied to).",
            )
        await button.click()  # a real click: opens the modal, or the employer's tab
        employer_page = await _wait_for_new_tab(page, known, timeout_ms=3000)
        if employer_page is not None:
            return employer_page
        await page.wait_for_timeout(1500)
        await self.guard(page)
        return page


async def _wait_for_new_tab(page: Page, known: set[Page], *, timeout_ms: int = 8000) -> Page | None:
    """The tab the site just opened, if any (it may already exist by now)."""
    for _ in range(timeout_ms // 250):
        fresh = [p for p in page.context.pages if p not in known and not p.is_closed()]
        if fresh:
            with contextlib.suppress(PlaywrightError):
                await fresh[-1].wait_for_load_state("domcontentloaded")
            return fresh[-1]
        await page.wait_for_timeout(250)
    return None
