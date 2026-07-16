from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobpilot.config.preferences import RankingWeights
from jobpilot.domain import Job, JobSource, MatchRecommendation, MatchResult, RemoteType
from jobpilot.matcher.ranking import rank_jobs


def make_pair(
    *,
    job_id: int,
    score: int,
    remote: RemoteType = RemoteType.REMOTE,
    salary_max: int | None = None,
    days_old: int | None = 1,
    technologies: list[str] | None = None,
) -> tuple[Job, MatchResult]:
    job = Job(
        id=job_id,
        title=f"Job {job_id}",
        company="Acme",
        application_url=f"https://a.com/{job_id}",
        source=JobSource.REMOTEOK,
        remote=remote,
        salary_max=salary_max,
        date_posted=(datetime.now(UTC) - timedelta(days=days_old)) if days_old else None,
        technologies=technologies or [],
    )
    match = MatchResult(
        job_id=job_id,
        resume_id=1,
        score=score,
        recommendation=MatchRecommendation.APPLY,
    )
    return job, match


class TestRankJobs:
    def test_higher_llm_score_wins(self) -> None:
        ranked = rank_jobs(
            [make_pair(job_id=1, score=60), make_pair(job_id=2, score=95)],
            weights=RankingWeights(),
        )
        assert [r.job.id for r in ranked] == [2, 1]

    def test_salary_preference_breaks_ties(self) -> None:
        ranked = rank_jobs(
            [
                make_pair(job_id=1, score=80, salary_max=50000),
                make_pair(job_id=2, score=80, salary_max=120000),
            ],
            weights=RankingWeights(),
            preferred_salary_min=100000,
        )
        assert ranked[0].job.id == 2

    def test_tech_overlap_breaks_ties(self) -> None:
        ranked = rank_jobs(
            [
                make_pair(job_id=1, score=80, technologies=["php"]),
                make_pair(job_id=2, score=80, technologies=["react", "typescript"]),
            ],
            weights=RankingWeights(),
            preferred_technologies=["react", "typescript"],
        )
        assert ranked[0].job.id == 2

    def test_fresh_posting_beats_stale(self) -> None:
        ranked = rank_jobs(
            [
                make_pair(job_id=1, score=80, days_old=44),
                make_pair(job_id=2, score=80, days_old=1),
            ],
            weights=RankingWeights(),
        )
        assert ranked[0].job.id == 2

    def test_composite_bounded_zero_one(self) -> None:
        ranked = rank_jobs(
            [make_pair(job_id=1, score=100, salary_max=200000, technologies=["react"])],
            weights=RankingWeights(),
            preferred_salary_min=100000,
            preferred_technologies=["react"],
        )
        assert 0.0 <= ranked[0].composite_score <= 1.0

    def test_zero_weights_do_not_crash(self) -> None:
        weights = RankingWeights(resume_match=0, salary=0, remote=0, recency=0, tech_overlap=0)
        ranked = rank_jobs([make_pair(job_id=1, score=80)], weights=weights)
        assert ranked[0].composite_score == 0.0
