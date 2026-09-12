"""OnlineJobs.ph — public search, account needed to apply.

Listings are server-rendered cards (`.jobpost-cat-box`) linking to
/jobseekers/job/<slug>-<id>. Employers are often anonymous, so a listing
without a company name falls back to a stable placeholder rather than
guessing one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page

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
    parse_salary,
    parse_timestamp,
)
from jobpilot.platforms.registry import register_platform

logger = logging.getLogger(__name__)

BASE = "https://www.onlinejobs.ph"
PAGE_SIZE = 30
ANONYMOUS_EMPLOYER = "Private employer (OnlineJobs.ph)"
_ID_IN_SLUG = re.compile(r"-(\d+)/?$")

_CARDS_JS = """
() => {
  const cards = document.querySelectorAll('.jobpost-cat-box, .latest-job-post');
  const text = (el, sel) => {
    const n = el.querySelector(sel);
    return n ? (n.innerText || '').replace(/\\s+/g, ' ').trim() : '';
  };
  return Array.from(cards).map((card) => {
    const link = card.querySelector('a[href*="/jobseekers/job/"]');
    return {
      url: link ? link.href : '',
      title: text(card, 'h4'),
      workType: text(card, '.badge'),
      posted: text(card, 'p.fs-13, .fs-13'),
      salary: text(card, 'dd'),
      teaser: text(card, '.desc'),
    };
  });
}
"""

_WORK_TYPES = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "gig": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
}


def employment_type(text: str) -> EmploymentType:
    lowered = clean(text).lower()
    for needle, value in _WORK_TYPES.items():
        if needle in lowered:
            return value
    return EmploymentType.UNKNOWN


def external_id(url: str) -> str | None:
    match = _ID_IN_SLUG.search(url.split("?")[0])
    return match.group(1) if match else None


@register_platform
class OnlineJobsAdapter(PlatformAdapter):
    platform: ClassVar[JobSource] = JobSource.ONLINEJOBS
    display_name: ClassVar[str] = "OnlineJobs.ph"
    home_url: ClassVar[str] = BASE
    login_url: ClassVar[str] = f"{BASE}/jobseekers/login"
    requires_login_to_search: ClassVar[bool] = False
    requires_login_to_apply: ClassVar[bool] = True
    logged_out_url_patterns: ClassVar[tuple[str, ...]] = ("/jobseekers/login", "/user/login")

    def search_url(self, query: SearchQuery, offset: int) -> str:
        path = "/jobseekers/jobsearch" + (f"/{offset}" if offset else "")
        return f"{BASE}{path}?jobkeyword={quote_plus(query.keywords)}"

    async def search(
        self, query: SearchQuery, browser: BrowserContext | None
    ) -> AsyncIterator[Job]:
        if browser is None:
            raise PlatformError(self.platform, "needs a browser context")
        page = await browser.new_page()
        try:
            seen = 0
            for offset in range(0, PAGE_SIZE * 10, PAGE_SIZE):
                await self.goto(page, self.search_url(query, offset))
                cards = as_dicts(await page.evaluate(_CARDS_JS))
                if not cards:
                    if offset == 0:
                        logger.warning("No OnlineJobs.ph cards matched on %s", page.url)
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

    def _to_job(self, card: dict[str, Any]) -> Job | None:
        url, title = clean(card.get("url")), clean(card.get("title"))
        job_id = external_id(url)
        if not url or not title or not job_id:
            return None
        salary_text = clean(card.get("salary"))
        salary_min, salary_max = parse_salary(salary_text)
        teaser = clean(card.get("teaser"))
        posted = clean(card.get("posted")).replace("Posted on", "").strip()
        return Job(
            title=title,
            company=ANONYMOUS_EMPLOYER,
            location="Remote" if "remote" in teaser.lower() else None,
            salary_raw=salary_text if salary_text.upper() != "TBD" else None,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type(f"{card.get('workType', '')} {teaser}"),
            # The whole board is remote work for Philippine-based workers.
            remote=RemoteType.REMOTE,
            description=teaser,
            application_url=url.split("?")[0],
            external_id=job_id,
            source=self.platform,
            date_posted=parse_timestamp(posted),
        )

    async def enrich(self, job: Job, browser: BrowserContext | None) -> Job:
        if browser is None:
            return job
        page = await browser.new_page()
        try:
            await self.goto(page, job.application_url)
            description = await self._text(page, ["#job-description", ".job-desc", "main"])
            title = await self._text(page, ["h1.job__title", "h1"])
            salary = await self._section(page, "WAGE / SALARY")
            work_type = await self._section(page, "TYPE OF WORK")
            salary_min, salary_max = parse_salary(salary)
            return job.model_copy(
                update={
                    "title": title or job.title,
                    "description": description or job.description,
                    "salary_raw": salary if salary and salary.upper() != "TBD" else job.salary_raw,
                    "salary_min": salary_min or job.salary_min,
                    "salary_max": salary_max or job.salary_max,
                    "employment_type": employment_type(work_type) or job.employment_type,
                }
            ).reidentified()
        finally:
            await page.close()

    async def _text(self, page: Page, selectors: list[str]) -> str:
        value = await page.evaluate(TEXT_OF_JS, {"selectors": selectors, "root": None})
        return clean(value if isinstance(value, str) else "")

    async def _section(self, page: Page, label: str) -> str:
        """Value of a labelled block ("TYPE OF WORK", "WAGE / SALARY", …)."""
        value = await page.evaluate(
            """
            (label) => {
              for (const el of document.querySelectorAll('dt,dd,p,div,span,h3')) {
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text.toUpperCase().startsWith(label) && text.length > label.length) {
                  return text.slice(label.length).trim();
                }
              }
              return '';
            }
            """,
            label,
        )
        return clean(value if isinstance(value, str) else "")

    async def session_status(self, browser: BrowserContext) -> SessionStatus:
        """A logged-out dashboard lands on /error (not the login page), so the
        signal is a logout link, not the absence of a redirect."""
        page = await browser.new_page()
        try:
            await self.goto(page, f"{BASE}/jobseekers/dashboard")
            url = page.url.lower()
            if "/error" in url or any(p in url for p in self.logged_out_url_patterns):
                return SessionStatus.NEEDS_LOGIN
            logged_in = await page.evaluate(
                "() => !!document.querySelector('a[href*=\"logout\" i]')"
            )
            return SessionStatus.LOGGED_IN if logged_in else SessionStatus.NEEDS_LOGIN
        except SessionExpiredError:
            return SessionStatus.NEEDS_LOGIN
        except ChallengeError:
            return SessionStatus.BLOCKED
        finally:
            await page.close()

    async def looks_logged_in(self, browser: BrowserContext, page: Page) -> bool:
        if any(p in page.url.lower() for p in self.logged_out_url_patterns):
            return False
        cookies = {c["name"] for c in await browser.cookies()}
        return "ci_session" in cookies and "/jobseekers/" in page.url

    async def open_application(self, page: Page, job: Job) -> Page:
        await self.goto(page, job.application_url)
        body = (await page.inner_text("body")).lower()
        if "login or register as jobseeker" in body:
            raise SessionExpiredError(self.platform, page.url)
        apply_button = page.locator(
            "a:has-text('Apply'), button:has-text('Apply'), "
            "a[href*='application'], button[aria-label*='Apply' i]"
        ).first
        if not await apply_button.count():
            raise PlatformError(
                self.platform, "couldn't find the Apply button (the post may be closed)."
            )
        await apply_button.click()
        await page.wait_for_timeout(1500)
        return page
