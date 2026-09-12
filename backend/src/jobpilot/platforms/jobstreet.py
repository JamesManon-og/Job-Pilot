"""JobStreet (SEEK platform: ph.jobstreet.com and siblings).

Search works logged out; applying needs your account. Cards and detail pages
are read through SEEK's `data-automation` attributes, which are stable across
redesigns; every extraction has fallbacks and reports a clear error when the
layout no longer matches.

Set another country in config.yaml:
    platforms:
      jobstreet:
        options: {domain: "www.jobstreet.com.sg"}
"""

from __future__ import annotations

import logging
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
    ATTR_OF_JS,
    TEXT_OF_JS,
    as_dicts,
    clean,
    parse_relative_date,
    parse_salary,
)
from jobpilot.platforms.registry import register_platform

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = "ph.jobstreet.com"

_CARDS_JS = """
() => {
  const cards = document.querySelectorAll(
    'article[data-card-type="JobCard"], article[data-testid="job-card"], ' +
    '[data-automation="normalJob"]'
  );
  const text = (el, sel) => {
    const n = el.querySelector(sel);
    return n ? (n.innerText || '').replace(/\\s+/g, ' ').trim() : '';
  };
  return Array.from(cards).map((card) => {
    const link = card.querySelector('[data-automation="jobTitle"], a[href*="/job/"]');
    return {
      id: card.getAttribute('data-job-id') || '',
      url: link ? link.href : '',
      title: text(card, '[data-automation="jobTitle"]') || (link ? link.innerText.trim() : ''),
      company: text(card, '[data-automation="jobCompany"]'),
      location: text(card, '[data-automation="jobCardLocation"], [data-automation="jobLocation"]'),
      salary: text(card, '[data-automation="jobSalary"]'),
      posted: text(card, '[data-automation="jobListingDate"]'),
      workType: text(card, '[data-automation="jobWorkType"]'),
      teaser: text(card, '[data-automation="jobShortDescription"]'),
    };
  });
}
"""

_WORK_TYPES = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temp": EmploymentType.CONTRACT,
    "casual": EmploymentType.PART_TIME,
    "internship": EmploymentType.INTERNSHIP,
}


def employment_type(text: str) -> EmploymentType:
    lowered = clean(text).lower()
    for needle, value in _WORK_TYPES.items():
        if needle in lowered:
            return value
    return EmploymentType.UNKNOWN


def remote_type(*texts: str) -> RemoteType:
    blob = " ".join(texts).lower()
    if "hybrid" in blob:
        return RemoteType.HYBRID
    if "remote" in blob or "work from home" in blob:
        return RemoteType.REMOTE
    return RemoteType.UNKNOWN


@register_platform
class JobStreetAdapter(PlatformAdapter):
    platform: ClassVar[JobSource] = JobSource.JOBSTREET
    display_name: ClassVar[str] = "JobStreet"
    home_url: ClassVar[str] = f"https://{DEFAULT_DOMAIN}"
    login_url: ClassVar[str] = f"https://{DEFAULT_DOMAIN}/oauth/login/"
    requires_login_to_search: ClassVar[bool] = False
    requires_login_to_apply: ClassVar[bool] = True
    logged_out_url_patterns: ClassVar[tuple[str, ...]] = ("/oauth/login", "login.seek.com")

    @property
    def domain(self) -> str:
        return self.settings.options.get("domain", DEFAULT_DOMAIN)

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    def search_url(self, query: SearchQuery, page_number: int) -> str:
        params = [f"keywords={quote_plus(query.keywords)}"]
        if query.location:
            params.append(f"where={quote_plus(query.location)}")
        if query.posted_within_days:
            params.append(f"daterange={query.posted_within_days}")
        if page_number > 1:
            params.append(f"page={page_number}")
        return f"{self.base}/jobs?{'&'.join(params)}"

    def job_url(self, job_id: str) -> str:
        return f"{self.base}/job/{job_id}"

    async def search(
        self, query: SearchQuery, browser: BrowserContext | None
    ) -> AsyncIterator[Job]:
        if browser is None:
            raise PlatformError(self.platform, "needs a browser context")
        page = await browser.new_page()
        try:
            seen = 0
            for page_number in range(1, 11):  # SEEK paginates in 30s; 10 pages is plenty
                await self.goto(page, self.search_url(query, page_number))
                cards = as_dicts(await page.evaluate(_CARDS_JS))
                if not cards:
                    if page_number == 1:
                        logger.warning("No JobStreet cards matched on %s", page.url)
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
        title, company = clean(card.get("title")), clean(card.get("company"))
        job_id = clean(card.get("id"))
        if not title or not job_id:
            return None
        salary_min, salary_max = parse_salary(clean(card.get("salary")))
        return Job(
            title=title,
            company=company or "Private advertiser",
            location=clean(card.get("location")) or None,
            salary_raw=clean(card.get("salary")) or None,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type(card.get("workType", "")),
            remote=remote_type(card.get("location", ""), title, card.get("teaser", "")),
            application_url=self.job_url(job_id),
            external_id=job_id,
            source=self.platform,
            date_posted=parse_relative_date(clean(card.get("posted"))),
        )

    async def enrich(self, job: Job, browser: BrowserContext | None) -> Job:
        if browser is None:
            return job
        page = await browser.new_page()
        try:
            await self.goto(page, job.application_url)
            description = await self._text(
                page, ['[data-automation="jobAdDetails"]', "article", "main"]
            )
            work_type = await self._text(page, ['[data-automation="job-detail-work-type"]'])
            salary = await self._text(page, ['[data-automation="job-detail-salary"]'])
            location = await self._text(page, ['[data-automation="job-detail-location"]'])
            company = await self._text(page, ['[data-automation="advertiser-name"]'])
            salary_min, salary_max = parse_salary(salary or "")
            return job.model_copy(
                update={
                    "description": description or job.description,
                    "company": company or job.company,
                    "location": location or job.location,
                    "salary_raw": salary or job.salary_raw,
                    "salary_min": salary_min or job.salary_min,
                    "salary_max": salary_max or job.salary_max,
                    "employment_type": employment_type(work_type or "") or job.employment_type,
                    "remote": remote_type(location or "", job.title, description or ""),
                }
            ).reidentified()
        finally:
            await page.close()

    async def _text(self, page: Page, selectors: list[str], root: str | None = None) -> str:
        value = await page.evaluate(TEXT_OF_JS, {"selectors": selectors, "root": root})
        return clean(value if isinstance(value, str) else "")

    _SIGNED_OUT_JS = """
    () => {
      const markers = document.querySelectorAll(
        '[data-automation="sign-in-register"], [data-automation="sign in"], ' +
        '[data-automation="register for free"]'
      );
      for (const el of markers) {
        if ((el.innerText || '').trim() || el.getAttribute('href')) return true;
      }
      const auth = document.querySelector(
        '[data-automation="desktop-auth-links-wrapper"], ' +
        '[data-automation="mobile-auth-links-wrapper"]'
      );
      return auth ? /sign ?in|register/i.test(auth.innerText || '') : false;
    }
    """

    async def session_status(self, browser: BrowserContext) -> SessionStatus:
        """Logged out still renders the profile shell, so look for the header's
        sign-in / register links instead of assuming a redirect."""
        page = await browser.new_page()
        try:
            await self.goto(page, f"{self.base}/profile/me")
            if any(p in page.url.lower() for p in self.logged_out_url_patterns):
                return SessionStatus.NEEDS_LOGIN
            signed_out = await page.evaluate(self._SIGNED_OUT_JS)
            return SessionStatus.NEEDS_LOGIN if signed_out else SessionStatus.LOGGED_IN
        except SessionExpiredError:
            return SessionStatus.NEEDS_LOGIN
        except ChallengeError:
            return SessionStatus.BLOCKED
        finally:
            await page.close()

    async def looks_logged_in(self, browser: BrowserContext, page: Page) -> bool:
        cookies = {c["name"] for c in await browser.cookies()}
        return bool(cookies & {"JobseekerSessionId", "seek_sid", "sol_id"}) and not any(
            p in page.url.lower() for p in self.logged_out_url_patterns
        )

    async def open_application(self, page: Page, job: Job) -> Page:
        await self.goto(page, job.application_url)
        href = await page.evaluate(
            ATTR_OF_JS,
            {
                "selectors": ['[data-automation="job-detail-apply"]', 'a[href*="/apply"]'],
                "attribute": "href",
                "root": None,
            },
        )
        if not isinstance(href, str) or not href:
            raise PlatformError(
                self.platform,
                "couldn't find the apply button (the listing may be closed or external).",
            )
        target = href if href.startswith("http") else f"{self.base}{href}"
        if self.domain not in target:  # employer's own site: opens the external form
            await self.goto(page, target)
            return page
        await self.goto(page, target)
        return page
