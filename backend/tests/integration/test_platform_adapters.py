"""LinkedIn / JobStreet / OnlineJobs.ph adapters against fixture pages.

The fixtures reproduce each site's *structure* (the attributes and classes the
adapters key on), served from a routed browser context — no network, no
accounts. They cover parsing, session detection, opening the application, and
the safety rule that JobPilot only ever clicks the button that opens a form.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Route, async_playwright

from jobpilot.config.preferences import PlatformSettings
from jobpilot.domain import EmploymentType, JobSource, RemoteType
from jobpilot.domain.enums import SessionStatus
from jobpilot.platforms import ChallengeError, PlatformError, SearchQuery, SessionExpiredError
from jobpilot.platforms.jobstreet import JobStreetAdapter
from jobpilot.platforms.linkedin import LinkedInAdapter, job_id_from
from jobpilot.platforms.onlinejobs import ANONYMOUS_EMPLOYER, OnlineJobsAdapter, external_id
from jobpilot.platforms.parsing import parse_relative_date, parse_salary, parse_timestamp
from jobpilot.scrapers.http import RateLimiter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platforms"
QUERY = SearchQuery(keywords="react developer", location="Philippines", max_results=10)


def page_source(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


@pytest.fixture
async def context() -> AsyncIterator[BrowserContext]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        yield ctx
        await browser.close()


class Site:
    """Serves fixture pages for a host and records the URLs the adapter visited."""

    def __init__(self, routes: dict[str, str], *, default: str | None = None) -> None:
        self.routes = routes  # URL substring -> fixture name
        self.default = default
        self.visited: list[str] = []

    def _is_later_page(self, url: str) -> bool:
        return bool(
            re.search(r"[?&]page=[2-9]", url)
            or re.search(r"[?&]start=[1-9]", url)
            or re.search(r"/jobsearch/[1-9]", url)
        )

    async def handle(self, route: Route) -> None:
        url = route.request.url
        self.visited.append(url)
        if self._is_later_page(url):  # the board has no more results
            await route.fulfill(body=page_source("empty"), content_type="text/html")
            return
        for needle, fixture in self.routes.items():
            if needle in url:
                await route.fulfill(body=page_source(fixture), content_type="text/html")
                return
        body = page_source(self.default) if self.default else "<html><body>nothing</body></html>"
        await route.fulfill(body=body, content_type="text/html")


def instant(adapter: object) -> object:
    adapter.limiter = RateLimiter(min_interval_seconds=0)  # type: ignore[attr-defined]
    return adapter


class TestJobStreet:
    def adapter(self) -> JobStreetAdapter:
        return instant(JobStreetAdapter(PlatformSettings(enabled=True)))  # type: ignore[return-value]

    async def test_search_parses_cards(self, context: BrowserContext) -> None:
        site = Site({"/jobs?": "jobstreet_search"}, default="empty")
        await context.route("https://ph.jobstreet.com/**", site.handle)
        adapter = self.adapter()
        jobs = [job async for job in adapter.search(QUERY, context)]

        assert [j.external_id for j in jobs] == ["94548133", "94548134"]  # broken card skipped
        first = jobs[0]
        assert first.title == "Website Developer"
        assert first.company == "Acme Digital Inc"
        assert first.application_url == "https://ph.jobstreet.com/job/94548133"
        assert (first.salary_min, first.salary_max) == (50_000, 70_000)
        assert first.employment_type is EmploymentType.FULL_TIME
        assert first.remote is RemoteType.HYBRID  # from the teaser
        assert first.date_posted is not None
        assert jobs[1].remote is RemoteType.REMOTE
        assert "keywords=react+developer" in site.visited[0]
        assert "where=Philippines" in site.visited[0]

    async def test_enrich_adds_details(self, context: BrowserContext) -> None:
        site = Site({"/jobs?": "jobstreet_search", "/job/": "jobstreet_job"})
        await context.route("https://ph.jobstreet.com/**", site.handle)
        adapter = self.adapter()
        listing = [job async for job in adapter.search(QUERY, context)][0]
        job = await adapter.enrich(listing, context)
        assert "design system" not in job.description
        assert "build and maintain our websites" in job.description
        assert job.company == "Acme Digital Incorporated"
        assert job.external_id == listing.external_id
        assert job.dedup_hash == listing.dedup_hash  # identity is stable across enrichment

    async def test_open_application_follows_quick_apply(self, context: BrowserContext) -> None:
        site = Site({"/apply": "jobstreet_apply", "/job/": "jobstreet_job"})
        await context.route("https://ph.jobstreet.com/**", site.handle)
        adapter = self.adapter()
        from jobpilot.domain import Job

        job = Job(
            title="Website Developer",
            company="Acme",
            application_url="https://ph.jobstreet.com/job/94548133",
            external_id="94548133",
            source=JobSource.JOBSTREET,
        )
        page = await context.new_page()
        form_page = await adapter.open_application(page, job)
        assert form_page.url.endswith("/job/94548133/apply")
        assert await form_page.locator("#rn").count() == 1

    async def test_session_detection(self, context: BrowserContext) -> None:
        adapter = self.adapter()
        logged_out = Site({"/profile/me": "jobstreet_search"})  # page shows a sign-in link
        await context.route("https://ph.jobstreet.com/**", logged_out.handle)
        assert await adapter.session_status(context) is SessionStatus.NEEDS_LOGIN

        logged_in = Site({"/profile/me": "jobstreet_job"})  # no sign-in link
        await context.unroute("https://ph.jobstreet.com/**")
        await context.route("https://ph.jobstreet.com/**", logged_in.handle)
        assert await adapter.session_status(context) is SessionStatus.LOGGED_IN

    async def test_other_country_domain(self) -> None:
        adapter = JobStreetAdapter(
            PlatformSettings(enabled=True, options={"domain": "www.jobstreet.com.sg"})
        )
        assert adapter.job_url("1").startswith("https://www.jobstreet.com.sg/job/")


class TestOnlineJobs:
    def adapter(self) -> OnlineJobsAdapter:
        return instant(OnlineJobsAdapter(PlatformSettings(enabled=True)))  # type: ignore[return-value]

    async def test_search_parses_cards(self, context: BrowserContext) -> None:
        site = Site({"jobsearch": "onlinejobs_search"}, default="empty")
        await context.route("https://www.onlinejobs.ph/**", site.handle)
        jobs = [job async for job in self.adapter().search(QUERY, context)]

        assert [j.external_id for j in jobs] == ["1717193", "1717000"]
        first = jobs[0]
        assert first.title == "Full-Stack Web Developer (React)"
        assert first.company == ANONYMOUS_EMPLOYER  # employers are anonymous here
        assert first.remote is RemoteType.REMOTE
        assert first.employment_type is EmploymentType.FULL_TIME
        assert first.salary_raw == "$1,200/month"
        assert first.date_posted is not None and first.date_posted.year == 2026
        assert jobs[1].salary_raw is None  # "TBD" is not a salary

    async def test_enrich_reads_labelled_sections(self, context: BrowserContext) -> None:
        site = Site({"jobsearch": "onlinejobs_search", "/job/": "onlinejobs_job"})
        await context.route("https://www.onlinejobs.ph/**", site.handle)
        adapter = self.adapter()
        listing = [job async for job in adapter.search(QUERY, context)][0]
        job = await adapter.enrich(listing, context)
        assert ".NET and SQL" in job.description
        assert job.employment_type is EmploymentType.FULL_TIME

    async def test_apply_requires_login(self, context: BrowserContext) -> None:
        from jobpilot.domain import Job

        site = Site({"/job/": "onlinejobs_job_logged_out"})
        await context.route("https://www.onlinejobs.ph/**", site.handle)
        job = Job(
            title="Dev",
            company=ANONYMOUS_EMPLOYER,
            application_url="https://www.onlinejobs.ph/jobseekers/job/dev-1",
            external_id="1",
            source=JobSource.ONLINEJOBS,
        )
        page = await context.new_page()
        with pytest.raises(SessionExpiredError, match="jobpilot login onlinejobs"):
            await self.adapter().open_application(page, job)

    async def test_apply_reveals_the_form(self, context: BrowserContext) -> None:
        from jobpilot.domain import Job

        site = Site({"/job/": "onlinejobs_job"})
        await context.route("https://www.onlinejobs.ph/**", site.handle)
        job = Job(
            title="Dev",
            company=ANONYMOUS_EMPLOYER,
            application_url="https://www.onlinejobs.ph/jobseekers/job/dev-1",
            external_id="1",
            source=JobSource.ONLINEJOBS,
        )
        page = await context.new_page()
        form_page = await self.adapter().open_application(page, job)
        assert await form_page.locator("#msg").is_visible()

    def test_external_id_from_slug(self) -> None:
        assert external_id("/jobseekers/job/full-stack-1717193") == "1717193"
        assert external_id("https://www.onlinejobs.ph/jobseekers/job/x-42/") == "42"
        assert external_id("/jobseekers/employer/12/profile") is None


class TestLinkedIn:
    def adapter(self) -> LinkedInAdapter:
        return instant(LinkedInAdapter(PlatformSettings(enabled=True)))  # type: ignore[return-value]

    async def test_search_parses_cards(self, context: BrowserContext) -> None:
        site = Site({"/jobs/search": "linkedin_search"}, default="empty")
        await context.route("https://www.linkedin.com/**", site.handle)
        query = SearchQuery(
            keywords="react", location="Philippines", remote_only=True, max_results=5
        )
        jobs = [job async for job in self.adapter().search(query, context)]

        assert [j.external_id for j in jobs] == ["3912345678", "3912345679"]
        assert jobs[0].title == "Senior React Developer"
        assert jobs[0].company == "Acme Digital"
        assert jobs[0].application_url == "https://www.linkedin.com/jobs/view/3912345678"
        assert jobs[0].remote is RemoteType.REMOTE
        assert jobs[1].remote is RemoteType.HYBRID
        assert jobs[1].date_posted is not None
        assert "f_WT=2" in site.visited[0]  # remote filter
        assert "f_TPR=" not in site.visited[0]  # no date filter unless configured

    async def test_layout_change_is_reported_not_silent(self, context: BrowserContext) -> None:
        await context.route("https://www.linkedin.com/**", Site({}, default=None).handle)
        with pytest.raises(PlatformError, match="layout may have changed"):
            [job async for job in self.adapter().search(QUERY, context)]

    async def test_authwall_is_a_session_error(self, context: BrowserContext) -> None:
        site = Site({"linkedin.com": "linkedin_authwall"})
        await context.route("https://www.linkedin.com/**", site.handle)
        with pytest.raises(SessionExpiredError, match="jobpilot login linkedin"):
            [job async for job in self.adapter().search(QUERY, context)]
        assert await self.adapter().session_status(context) is SessionStatus.NEEDS_LOGIN

    async def test_checkpoint_is_a_challenge_never_bypassed(self, context: BrowserContext) -> None:
        async def checkpoint(route: Route) -> None:
            await route.fulfill(
                status=302,
                headers={"Location": "https://www.linkedin.com/checkpoint/challenge/verify"},
            )

        await context.route("https://www.linkedin.com/jobs/**", checkpoint)
        await context.route(
            "https://www.linkedin.com/checkpoint/**",
            lambda route: route.fulfill(body="<h1>Verify</h1>", content_type="text/html"),
        )
        adapter = self.adapter()
        with pytest.raises(ChallengeError, match="won't bypass"):
            [job async for job in adapter.search(QUERY, context)]
        assert await adapter.session_status(context) is SessionStatus.BLOCKED

    async def test_enrich_reads_description_and_insights(self, context: BrowserContext) -> None:
        site = Site({"/jobs/search": "linkedin_search", "/jobs/view/": "linkedin_job"})
        await context.route("https://www.linkedin.com/**", site.handle)
        adapter = self.adapter()
        listing = [job async for job in adapter.search(QUERY, context)][0]
        job = await adapter.enrich(listing, context)
        assert "design system" in job.description
        assert job.company == "Acme Digital Inc."
        assert job.employment_type is EmploymentType.FULL_TIME
        assert job.remote is RemoteType.REMOTE

    async def test_easy_apply_opens_the_form_but_never_submits(
        self, context: BrowserContext
    ) -> None:
        from jobpilot.domain import Job

        site = Site({"/jobs/view/": "linkedin_job"})
        await context.route("https://www.linkedin.com/**", site.handle)
        job = Job(
            title="Senior React Developer",
            company="Acme",
            application_url="https://www.linkedin.com/jobs/view/3912345678",
            external_id="3912345678",
            source=JobSource.LINKEDIN,
        )
        page = await context.new_page()
        form_page = await self.adapter().open_application(page, job)
        assert await form_page.evaluate("window.__easyApplyOpened === true")
        assert await form_page.locator("#ln").is_visible()
        # The submit button was never touched, and neither was "Next".
        assert await form_page.evaluate("window.__submitted === true") is False

    async def test_external_apply_returns_the_employers_tab(self, context: BrowserContext) -> None:
        from jobpilot.domain import Job

        site = Site({"/jobs/view/": "linkedin_job_external"})
        await context.route("https://www.linkedin.com/**", site.handle)
        await context.route(
            "https://employer.test/**",
            lambda route: route.fulfill(
                body='<label for="e">Email</label><input id="e">', content_type="text/html"
            ),
        )
        job = Job(
            title="Backend Engineer",
            company="Gamma",
            application_url="https://www.linkedin.com/jobs/view/3912345680",
            external_id="3912345680",
            source=JobSource.LINKEDIN,
        )
        page = await context.new_page()
        form_page = await self.adapter().open_application(page, job)
        assert form_page.url.startswith("https://employer.test/")
        assert await form_page.locator("#e").count() == 1

    async def test_missing_apply_button_is_a_clear_error(self, context: BrowserContext) -> None:
        from jobpilot.domain import Job

        await context.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(body="<h1>Closed</h1>", content_type="text/html"),
        )
        job = Job(
            title="Gone",
            company="Acme",
            application_url="https://www.linkedin.com/jobs/view/1",
            external_id="1",
            source=JobSource.LINKEDIN,
        )
        page = await context.new_page()
        with pytest.raises(PlatformError, match="no Apply button"):
            await self.adapter().open_application(page, job)

    def test_job_id_extraction(self) -> None:
        assert job_id_from("https://www.linkedin.com/jobs/view/3912345678/?refId=x") == "3912345678"
        assert job_id_from("", "urn:li:jobPosting:123") == "123"
        assert job_id_from("/jobs/search/?currentJobId=456") == "456"
        assert job_id_from("/jobs/view/senior-react-developer-at-acme-789") == "789"
        assert job_id_from("https://example.com/jobs") is None


class TestParsingHelpers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("₱50,000 – ₱70,000 per month", (50_000, 70_000)),
            ("$1,200/month", (1200, None)),
            ("Up to ₱90,000", (90_000, None)),
            ("Competitive", (None, None)),
            ("40 hours", (None, None)),  # too small to be a salary
        ],
    )
    def test_salary(self, text: str, expected: tuple[int | None, int | None]) -> None:
        assert parse_salary(text) == expected

    @pytest.mark.parametrize("text", ["3d ago", "Posted 3 days ago", "3 days ago"])
    def test_relative_dates(self, text: str) -> None:
        from datetime import UTC, datetime

        now = datetime(2026, 9, 12, tzinfo=UTC)
        parsed = parse_relative_date(text, now=now)
        assert parsed is not None and parsed.day == 9

    def test_timestamps(self) -> None:
        assert parse_timestamp("2026-09-10 08:49:24") is not None
        assert parse_timestamp("Sep 10, 2026") is not None
        assert parse_timestamp("2026-09-08") is not None
        assert parse_timestamp("not a date") is None
