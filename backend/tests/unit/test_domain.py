from __future__ import annotations

import pytest

from jobpilot.domain import Job, JobSource, compute_dedup_hash


class TestDedupHash:
    def test_stable(self) -> None:
        a = compute_dedup_hash("Acme", "Engineer", "https://a.com/j/1")
        b = compute_dedup_hash("Acme", "Engineer", "https://a.com/j/1")
        assert a == b

    def test_normalizes_case_and_whitespace(self) -> None:
        a = compute_dedup_hash("Acme", "Engineer", "https://a.com/j/1")
        b = compute_dedup_hash("  ACME ", " engineer", "HTTPS://A.COM/J/1  ")
        assert a == b

    def test_distinct_jobs_differ(self) -> None:
        a = compute_dedup_hash("Acme", "Engineer", "https://a.com/j/1")
        b = compute_dedup_hash("Acme", "Senior Engineer", "https://a.com/j/2")
        assert a != b


class TestJobModel:
    def test_dedup_hash_autofilled(self) -> None:
        job = Job(
            title="Engineer",
            company="Acme",
            application_url="https://a.com/j/1",
            source=JobSource.REMOTEOK,
        )
        assert job.dedup_hash == compute_dedup_hash("Acme", "Engineer", "https://a.com/j/1")

    def test_requires_title_and_company(self) -> None:
        with pytest.raises(ValueError):
            Job(title="", company="Acme", application_url="u", source=JobSource.REMOTEOK)
        with pytest.raises(ValueError):
            Job(title="Engineer", company="", application_url="u", source=JobSource.REMOTEOK)
