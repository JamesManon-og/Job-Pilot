from __future__ import annotations

import pytest

from jobpilot.domain import Job, JobSource
from jobpilot.domain.identity import (
    canonicalize_url,
    identity_hash,
    job_fingerprint,
    normalize_company,
    normalize_title,
)


def job(**kwargs: object) -> Job:
    fields: dict[str, object] = {
        "title": "Engineer",
        "company": "Acme",
        "application_url": "https://a.com/j/1",
        "source": JobSource.REMOTEOK,
    }
    fields.update(kwargs)
    return Job.model_validate(fields)


class TestIdentity:
    def test_identity_survives_title_edits(self) -> None:
        """The old dedup_hash hashed company+title+url, so editing a title made
        a "new" posting and crashed re-scrapes. Identity is the posting itself."""
        assert job(title="Engineer").dedup_hash == job(title="Senior Engineer!").dedup_hash

    def test_tracking_parameters_do_not_change_identity(self) -> None:
        a = job(application_url="https://www.linkedin.com/jobs/view/123/?refId=x&trackingId=y")
        b = job(application_url="https://www.LinkedIn.com/jobs/view/123?utm_source=feed#top")
        assert a.canonical_url == b.canonical_url == "https://www.linkedin.com/jobs/view/123"
        assert a.dedup_hash == b.dedup_hash

    def test_meaningful_query_parameters_are_kept(self) -> None:
        a = canonicalize_url("https://boards.example.com/apply?gh_jid=42&utm_medium=x")
        b = canonicalize_url("https://boards.example.com/apply?gh_jid=43")
        assert a == "https://boards.example.com/apply?gh_jid=42"
        assert a != b

    def test_external_id_wins_over_url(self) -> None:
        a = job(external_id="987", application_url="https://jobs.test/a")
        b = job(external_id="987", application_url="https://jobs.test/b")
        assert a.dedup_hash == b.dedup_hash == identity_hash("remoteok", "987", a.canonical_url)

    def test_same_id_on_different_platforms_is_different(self) -> None:
        a = job(external_id="1", source=JobSource.LINKEDIN)
        b = job(external_id="1", source=JobSource.JOBSTREET)
        assert a.dedup_hash != b.dedup_hash

    def test_blank_external_id_is_none(self) -> None:
        assert job(external_id="  ").external_id is None


class TestFingerprint:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            (("Acme Inc.", "Senior React Developer"), ("ACME", "Sr. React Developer")),
            (("Acme Philippines", "Engineer (Remote)"), ("Acme", "Engineer - Remote")),
            (("Acme Corp", "URGENT HIRING: Engineer"), ("acme", "Engineer")),
            (("Acme", "Engineer | Work From Home"), ("Acme", "Engineer")),
        ],
    )
    def test_same_job_on_different_boards(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        assert job_fingerprint(*a) == job_fingerprint(*b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            (("Acme", "Engineer - Backend"), ("Acme", "Engineer - Frontend")),
            (("Acme", "Software Engineer (Backend)"), ("Acme", "Software Engineer")),
            (("Acme", "Engineer"), ("Acme Labs", "Engineer")),
            (("Acme", "Senior Engineer"), ("Acme", "Junior Engineer")),
        ],
    )
    def test_different_jobs_stay_different(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        assert job_fingerprint(*a) != job_fingerprint(*b)

    def test_normalizers(self) -> None:
        assert normalize_company("  Globe Telecom, Inc. ") == "globe telecom"
        assert normalize_title("Sr. C# / .NET Developer (Hybrid)") == "senior c# net developer"


class TestJobModel:
    def test_identity_fields_autofilled(self) -> None:
        j = job()
        assert j.canonical_url == "https://a.com/j/1"
        assert len(j.dedup_hash) == 64 and len(j.fingerprint) == 32

    def test_requires_title_and_company(self) -> None:
        with pytest.raises(ValueError):
            Job(
                title="",
                company="Acme",
                application_url="https://u.test",
                source=JobSource.REMOTEOK,
            )
        with pytest.raises(ValueError):
            Job(
                title="Engineer",
                company="",
                application_url="https://u.test",
                source=JobSource.REMOTEOK,
            )

    def test_non_http_urls_rejected(self) -> None:
        with pytest.raises(ValueError, match="http"):
            job(application_url="javascript:alert(1)")
