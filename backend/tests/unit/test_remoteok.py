from __future__ import annotations

from jobpilot.domain import JobSource, RemoteType
from jobpilot.scrapers.remoteok import _parse_entry

FULL_ENTRY = {
    "slug": "remote-senior-frontend-engineer-acme-123",
    "id": "123",
    "epoch": 1752600000,
    "date": "2025-07-15T12:00:00-07:00",
    "company": "Acme",
    "position": "Senior Frontend Engineer",
    "tags": ["React", "TypeScript ", "next.js"],
    "description": "<p>Build <b>great</b> things.</p><ul><li>React</li></ul>",
    "location": "Worldwide",
    "salary_min": 90000,
    "salary_max": 130000,
    "apply_url": "https://remoteok.com/l/123",
    "url": "https://remoteok.com/remote-jobs/123",
}


class TestParseEntry:
    def test_full_entry(self) -> None:
        job = _parse_entry(FULL_ENTRY)
        assert job is not None
        assert job.title == "Senior Frontend Engineer"
        assert job.company == "Acme"
        assert job.source is JobSource.REMOTEOK
        assert job.remote is RemoteType.REMOTE
        assert job.technologies == ["react", "typescript", "next.js"]
        assert job.salary_min == 90000 and job.salary_max == 130000
        assert job.application_url == "https://remoteok.com/remote-jobs/123"
        assert job.date_posted is not None and job.date_posted.year >= 2025
        assert "<p>" not in job.description and "great" in job.description

    def test_missing_required_fields_returns_none(self) -> None:
        assert _parse_entry({**FULL_ENTRY, "position": ""}) is None
        assert _parse_entry({**FULL_ENTRY, "company": None}) is None
        assert _parse_entry({**FULL_ENTRY, "url": "", "apply_url": ""}) is None

    def test_falls_back_to_apply_url(self) -> None:
        job = _parse_entry({**FULL_ENTRY, "url": ""})
        assert job is not None
        assert job.application_url == "https://remoteok.com/l/123"

    def test_bad_salary_and_epoch_tolerated(self) -> None:
        job = _parse_entry(
            {**FULL_ENTRY, "salary_min": "n/a", "salary_max": None, "epoch": "bogus"}
        )
        assert job is not None
        assert job.salary_min is None
        assert job.date_posted is None


class TestMalformedData:
    """One broken listing must not kill the scrape (audit finding)."""

    def test_non_string_fields_are_rejected_not_crashing(self) -> None:
        """Bug: `(123).strip()` raised AttributeError inside the generator,
        aborting every listing after it."""
        assert _parse_entry({**FULL_ENTRY, "position": 123}) is None
        assert _parse_entry({**FULL_ENTRY, "company": ["Acme"]}) is None

    def test_tags_as_a_string_are_split_not_iterated(self) -> None:
        """Bug: "python" became technologies ['p','y','t','h','o','n']."""
        job = _parse_entry({**FULL_ENTRY, "tags": "python, react"})
        assert job is not None
        assert job.technologies == ["python", "react"]

    def test_non_http_urls_are_rejected(self) -> None:
        assert _parse_entry({**FULL_ENTRY, "url": "javascript:alert(1)", "apply_url": ""}) is None

    async def test_scraper_skips_bad_entries_and_keeps_going(self) -> None:
        import httpx

        from jobpilot.scrapers import remoteok

        payload = [
            {"legal": "notice"},
            {**FULL_ENTRY, "id": "1", "url": "https://remoteok.com/1"},
            {**FULL_ENTRY, "id": "2", "position": {"nested": "junk"}},
            "not even a dict",
            {**FULL_ENTRY, "id": "3", "url": "https://remoteok.com/3", "epoch": 10**20},
            {**FULL_ENTRY, "id": "4", "url": "https://remoteok.com/4"},
        ]
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        original = remoteok.create_http_client
        remoteok.create_http_client = lambda **_: httpx.AsyncClient(transport=transport)  # type: ignore[assignment]
        try:
            jobs = [job async for job in remoteok.RemoteOKScraper().scrape()]
        finally:
            remoteok.create_http_client = original  # type: ignore[assignment]
        assert [j.application_url for j in jobs] == [
            "https://remoteok.com/1",
            "https://remoteok.com/3",
            "https://remoteok.com/4",
        ]
