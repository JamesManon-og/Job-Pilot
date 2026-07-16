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
