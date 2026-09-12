"""Weighted composite ranking of matched jobs.

The LLM match score is the core signal; preference alignment (salary, remote,
recency, tech overlap) nudges the final ranking. Weights are configurable via
the `ranking_weights` section of config.yaml.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from jobpilot.config.preferences import RankingWeights
from jobpilot.domain.enums import RemoteType
from jobpilot.domain.models import Job, MatchResult


class RankedJob(BaseModel):
    job: Job
    match: MatchResult
    composite_score: float


def _salary_score(job: Job, preferred_min: int | None) -> float:
    if preferred_min is None or not (job.salary_min or job.salary_max):
        return 0.5  # unknown either way: neutral
    best = job.salary_max or job.salary_min or 0
    if best >= preferred_min:
        return 1.0
    return max(0.0, best / preferred_min)


def _remote_score(job: Job) -> float:
    return {
        RemoteType.REMOTE: 1.0,
        RemoteType.HYBRID: 0.6,
        RemoteType.UNKNOWN: 0.5,
        RemoteType.ONSITE: 0.0,
    }[job.remote]


def _recency_score(job: Job, *, now: datetime | None = None) -> float:
    if job.date_posted is None:
        return 0.5
    now = now or datetime.now(UTC)
    posted = job.date_posted
    if posted.tzinfo is None:  # scrapers may emit naive UTC datetimes
        posted = posted.replace(tzinfo=UTC)
    age_days = max(0.0, (now - posted).total_seconds() / 86400)
    if age_days <= 3:
        return 1.0
    if age_days >= 45:
        return 0.0
    return 1.0 - (age_days - 3) / 42


def _tech_overlap_score(job: Job, preferred: list[str]) -> float:
    if not preferred:
        return 0.5
    job_techs = {t.strip().lower() for t in job.technologies}
    if not job_techs:
        return 0.5
    wanted = {t.strip().lower() for t in preferred}
    return len(job_techs & wanted) / len(wanted)


def rank_jobs(
    pairs: list[tuple[Job, MatchResult]],
    *,
    weights: RankingWeights,
    preferred_salary_min: int | None = None,
    preferred_technologies: list[str] | None = None,
) -> list[RankedJob]:
    """Return jobs sorted by composite score, best first."""
    total_weight = (
        weights.resume_match
        + weights.salary
        + weights.remote
        + weights.recency
        + weights.tech_overlap
    ) or 1.0
    ranked: list[RankedJob] = []
    for job, match in pairs:
        composite = (
            weights.resume_match * (match.score / 100)
            + weights.salary * _salary_score(job, preferred_salary_min)
            + weights.remote * _remote_score(job)
            + weights.recency * _recency_score(job)
            + weights.tech_overlap * _tech_overlap_score(job, preferred_technologies or [])
        ) / total_weight
        ranked.append(RankedJob(job=job, match=match, composite_score=round(composite, 4)))
    ranked.sort(key=lambda r: r.composite_score, reverse=True)
    return ranked
