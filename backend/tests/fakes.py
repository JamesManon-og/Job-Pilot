"""Fake platform adapters and browser profiles for runner/pipeline tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from jobpilot.domain import Job, JobSource, RemoteType
from jobpilot.domain.enums import SessionStatus
from jobpilot.platforms.base import PlatformAdapter, SearchQuery, matches_keywords


def make_job(
    i: int,
    *,
    company: str = "Acme",
    title: str | None = None,
    source: JobSource = JobSource.OTHER,
    remote: RemoteType = RemoteType.REMOTE,
    **extra: Any,
) -> Job:
    return Job(
        title=title or f"Engineer {i}",
        company=f"{company}{i}",
        application_url=f"https://jobs.test/{source.value}/{i}",
        external_id=str(i),
        source=source,
        remote=remote,
        **extra,
    )


class FakeAdapter(PlatformAdapter):
    """API-style adapter serving a fixed list; records every enrich call."""

    platform: ClassVar[JobSource] = JobSource.OTHER
    display_name: ClassVar[str] = "Fake"
    home_url: ClassVar[str] = "https://jobs.test"
    uses_browser: ClassVar[bool] = False

    def __init__(
        self,
        jobs: list[Job] | None = None,
        *,
        fail_enrich: Callable[[Job], bool] = lambda job: False,
        fail_query: str | None = None,
        raise_in_search: Exception | None = None,
    ) -> None:
        super().__init__()
        self.jobs = jobs or []
        self.enriched: list[str | None] = []
        self.fail_enrich = fail_enrich
        self.fail_query = fail_query
        self.raise_in_search = raise_in_search

    async def search(self, query: SearchQuery, browser: object) -> AsyncIterator[Job]:
        if self.raise_in_search is not None:
            raise self.raise_in_search
        if query.keywords == self.fail_query:
            raise RuntimeError(f"results page for {query.keywords!r} changed layout")
        for job in self.jobs:
            if matches_keywords(query.keywords, job.title):
                yield job.model_copy(update={"description": ""})  # listing: no details

    async def enrich(self, job: Job, browser: object) -> Job:
        self.enriched.append(job.external_id)
        if self.fail_enrich(job):
            raise RuntimeError("detail page timed out")
        original = next(j for j in self.jobs if j.external_id == job.external_id)
        return original.model_copy(update={"description": original.description or "Details"})


class FakeBrowserAdapter(FakeAdapter):
    """Like FakeAdapter but declares a browser + login, with a scripted session state."""

    platform: ClassVar[JobSource] = JobSource.LINKEDIN
    uses_browser: ClassVar[bool] = True
    login_url: ClassVar[str | None] = "https://jobs.test/login"
    requires_login_to_search: ClassVar[bool] = True

    def __init__(self, *args: Any, session: SessionStatus = SessionStatus.LOGGED_IN, **kw: Any):
        super().__init__(*args, **kw)
        self.session = session

    async def session_status(self, browser: object) -> SessionStatus:  # type: ignore[override]
        return self.session


class FakeProfiles:
    """Stands in for BrowserProfiles: records which profiles were opened."""

    def __init__(self) -> None:
        self.opened: list[JobSource] = []

    @asynccontextmanager
    async def open(
        self, platform: JobSource, *, headless: bool | None = None
    ) -> AsyncIterator[object]:
        self.opened.append(platform)
        yield object()
