"""Pure domain entities. No imports from infrastructure layers."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from jobpilot.domain.enums import (
    ApplicationStatus,
    EmploymentType,
    ExperienceLevel,
    JobSource,
    MatchRecommendation,
    RemoteType,
    ScrapeRunStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def compute_dedup_hash(company: str, title: str, application_url: str) -> str:
    """Stable fingerprint for duplicate detection across scrape runs and sources.

    Normalization (strip + casefold) means the same posting scraped twice —
    or with cosmetic differences in casing/whitespace — hashes identically.
    """
    normalized = "|".join(part.strip().casefold() for part in (company, title, application_url))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Job(BaseModel):
    id: int | None = None
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str | None = None
    salary_raw: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience_level: ExperienceLevel = ExperienceLevel.UNKNOWN
    remote: RemoteType = RemoteType.UNKNOWN
    visa_sponsorship: bool | None = None
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    application_url: str = Field(min_length=1)
    source: JobSource
    date_posted: datetime | None = None
    scraped_at: datetime = Field(default_factory=utcnow)
    dedup_hash: str = ""

    @field_validator("application_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        # Scraped data is untrusted: the dashboard renders this as a link, so a
        # javascript:/data: URL must never get in.
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError(f"application_url must be an http(s) URL, got {value[:60]!r}")
        return value

    @model_validator(mode="after")
    def _fill_dedup_hash(self) -> Job:
        if not self.dedup_hash:
            self.dedup_hash = compute_dedup_hash(self.company, self.title, self.application_url)
        return self


class ResumeProfile(BaseModel):
    """Structured content extracted from a resume file (Milestone 5)."""

    skills: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    years_experience: float | None = None
    ats_keywords: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    id: int | None = None
    version: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    profile: ResumeProfile = Field(default_factory=ResumeProfile)
    is_active: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class MatchResult(BaseModel):
    id: int | None = None
    job_id: int
    resume_id: int
    score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    recommendation: MatchRecommendation
    reasoning: str = ""
    llm_model: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# The only legal application lifecycle moves. Every status change goes through
# ApplicationRepository.transition(), which enforces this table atomically, so
# no code path can skip human approval or submit the same application twice.
APPLICATION_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.PENDING_REVIEW: frozenset(
        {ApplicationStatus.APPROVED, ApplicationStatus.REJECTED, ApplicationStatus.SKIPPED}
    ),
    ApplicationStatus.APPROVED: frozenset(
        {
            ApplicationStatus.AWAITING_CONFIRMATION,
            ApplicationStatus.PENDING_REVIEW,
            ApplicationStatus.REJECTED,
        }
    ),
    ApplicationStatus.AWAITING_CONFIRMATION: frozenset(
        {
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.APPROVED,
            ApplicationStatus.PENDING_REVIEW,
            ApplicationStatus.FAILED,
            ApplicationStatus.REJECTED,
        }
    ),
    ApplicationStatus.FAILED: frozenset({ApplicationStatus.APPROVED, ApplicationStatus.REJECTED}),
    ApplicationStatus.SUBMITTED: frozenset(),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.SKIPPED: frozenset(),
}


def can_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    return target in APPLICATION_TRANSITIONS[current]


class Application(BaseModel):
    id: int | None = None
    job_id: int
    resume_id: int
    status: ApplicationStatus = ApplicationStatus.PENDING_REVIEW
    cover_letter: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    screenshot_path: str | None = None
    external_application_id: str | None = None
    notes: str = ""
    submitted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ScrapeRun(BaseModel):
    id: int | None = None
    source: JobSource
    status: ScrapeRunStatus = ScrapeRunStatus.RUNNING
    jobs_found: int = 0
    jobs_new: int = 0
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
