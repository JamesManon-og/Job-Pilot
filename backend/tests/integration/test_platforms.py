"""RemoteOK adapter filtering and real persistent browser profiles."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobpilot.domain import Job, JobSource
from jobpilot.platforms import BrowserProfiles, ProfileInUseError, SearchQuery, matches_keywords
from jobpilot.platforms.remoteok import RemoteOKAdapter


class StubScraper:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = jobs
        self.calls = 0

    async def scrape(self) -> AsyncIterator[Job]:
        self.calls += 1
        for job in self.jobs:
            yield job


def remote_job(i: int, title: str, tags: list[str]) -> Job:
    return Job(
        title=title,
        company=f"Co{i}",
        application_url=f"https://remoteok.com/remote-jobs/{i}",
        external_id=str(i),
        source=JobSource.REMOTEOK,
        technologies=tags,
    )


class TestRemoteOKAdapter:
    async def test_filters_by_query_and_fetches_the_feed_once(self) -> None:
        stub = StubScraper(
            [
                remote_job(1, "Senior Frontend Engineer", ["react", "typescript"]),
                remote_job(2, "Customer Support Agent", ["support"]),
                remote_job(3, "Full-Stack Developer", ["node"]),
            ]
        )
        adapter = RemoteOKAdapter(scraper=stub)  # type: ignore[arg-type]
        react = [
            j.external_id
            async for j in adapter.search(SearchQuery(keywords="react developer"), None)
        ]
        fullstack = [
            j.external_id
            async for j in adapter.search(SearchQuery(keywords="full stack developer"), None)
        ]
        everything = [j.external_id async for j in adapter.search(SearchQuery(keywords="*"), None)]
        assert react == ["1"]
        assert fullstack == ["3"]
        assert everything == ["1", "2", "3"]
        assert stub.calls == 1  # one API request per run, not per query

    async def test_max_results(self) -> None:
        stub = StubScraper([remote_job(i, "React Developer", []) for i in range(1, 10)])
        adapter = RemoteOKAdapter(scraper=stub)  # type: ignore[arg-type]
        got = [j async for j in adapter.search(SearchQuery(keywords="react", max_results=3), None)]
        assert len(got) == 3


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("react developer", "Senior React Engineer", True),
        ("react", "Reactive Systems Engineer", False),
        ("full stack", "Fullstack Engineer", True),
        ("c#", "Senior C# Developer.", True),
        ("next.js", "Next.js / React Developer", True),
        ("vue", "Senior React Developer", False),
        ("developer", "Backend Developer", True),
    ],
)
def test_keyword_matching(query: str, text: str, expected: bool) -> None:
    assert matches_keywords(query, text) is expected


class TestBrowserProfiles:
    async def test_profile_is_private_and_exclusive(self, tmp_path: Path) -> None:
        profiles = BrowserProfiles(tmp_path / "profiles", tmp_path / "locks")
        async with profiles.open(JobSource.LINKEDIN) as context:
            path = profiles.profile_path(JobSource.LINKEDIN)
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert stat.S_IMODE((tmp_path / "profiles").stat().st_mode) == 0o700
            with pytest.raises(ProfileInUseError):
                async with profiles.open(JobSource.LINKEDIN):
                    pass
            # Other platforms are independent.
            async with profiles.open(JobSource.JOBSTREET):
                pass
            await context.new_page()
        assert profiles.has_profile(JobSource.LINKEDIN)

    async def test_logins_survive_restarts_including_session_cookies(self, tmp_path: Path) -> None:
        """Chromium drops cookies without an expiry on exit; boards that use
        them would log the user out after every run."""
        import time

        profiles = BrowserProfiles(tmp_path / "profiles", tmp_path / "locks")
        session_cookie = {"name": "sid", "value": "abc", "url": "https://jobs.test/"}
        persistent = {
            "name": "remember",
            "value": "xyz",
            "url": "https://jobs.test/",
            "expires": time.time() + 86_400,
        }
        async with profiles.open(JobSource.LINKEDIN) as context:
            await context.add_cookies([session_cookie, persistent])  # type: ignore[list-item]
        state = profiles.profile_path(JobSource.LINKEDIN) / "jobpilot-session-state.json"
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        async with profiles.open(JobSource.LINKEDIN) as context:
            names = sorted(c["name"] for c in await context.cookies("https://jobs.test/"))
        assert names == ["remember", "sid"]

        assert profiles.delete_profile(JobSource.LINKEDIN) is True
        assert not profiles.has_profile(JobSource.LINKEDIN)
