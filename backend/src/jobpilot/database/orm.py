"""SQLAlchemy ORM table definitions.

Enum-valued columns are stored as plain strings (the domain enums' values)
to keep SQLite schemas and Alembic migrations simple; conversion to/from
domain enums happens in the repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on top of SQLite's naive storage.

    SQLite has no timezone type: SQLAlchemy stores the wall-clock value and
    returns naive datetimes, which crash arithmetic against ``datetime.now(UTC)``
    and silently shift non-UTC inputs. Normalize to UTC on write and re-attach
    UTC on read so the rest of the app only ever sees aware datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)  # naive values are UTC by convention
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, str]: JSON,
        list[str]: JSON,
        datetime: UTCDateTime(),
    }


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # dedup_hash = hash of (source, external_id) or canonical URL: one row per posting.
        UniqueConstraint("dedup_hash", name="uq_jobs_dedup_hash"),
        Index("uq_jobs_source_external_id", "source", "external_id", unique=True),
        Index("ix_jobs_application_url", "application_url"),
        Index("ix_jobs_canonical_url", "canonical_url"),
        Index("ix_jobs_fingerprint", "fingerprint"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_source", "source"),
        Index("ix_jobs_company", "company"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    salary_raw: Mapped[str | None] = mapped_column(String(200))
    salary_min: Mapped[int | None]
    salary_max: Mapped[int | None]
    employment_type: Mapped[str] = mapped_column(String(30))
    experience_level: Mapped[str] = mapped_column(String(30))
    remote: Mapped[str] = mapped_column(String(30))
    visa_sponsorship: Mapped[bool | None]
    description: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[list[str]] = mapped_column(default=list)
    benefits: Mapped[list[str]] = mapped_column(default=list)
    technologies: Mapped[list[str]] = mapped_column(default=list)
    application_url: Mapped[str] = mapped_column(String(2000))
    source: Mapped[str] = mapped_column(String(30))
    date_posted: Mapped[datetime | None]
    scraped_at: Mapped[datetime]
    dedup_hash: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(200))
    canonical_url: Mapped[str] = mapped_column(String(2000), default="")
    fingerprint: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(20), default="discovered")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    last_seen_at: Mapped[datetime | None]


class ResumeRow(Base):
    __tablename__ = "resumes"
    __table_args__ = (UniqueConstraint("version", name="uq_resumes_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(100))
    file_path: Mapped[str] = mapped_column(String(1000))
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]


class MatchResultRow(Base):
    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("job_id", "resume_id", name="uq_match_results_job_resume"),
        Index("ix_match_results_score", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    score: Mapped[int]
    matched_skills: Mapped[list[str]] = mapped_column(default=list)
    missing_skills: Mapped[list[str]] = mapped_column(default=list)
    recommendation: Mapped[str] = mapped_column(String(30))
    reasoning: Mapped[str] = mapped_column(Text, default="")
    llm_model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime]


class ApplicationRow(Base):
    __tablename__ = "applications"
    __table_args__ = (
        # One application per job, ever — schema-level duplicate prevention.
        UniqueConstraint("job_id", name="uq_applications_job_id"),
        Index("ix_applications_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(30))
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    answers: Mapped[dict[str, str]] = mapped_column(default=dict)
    screenshot_path: Mapped[str | None] = mapped_column(String(1000))
    external_application_id: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class ApplicationEventRow(Base):
    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_application_id", "application_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]


class ScrapeRunRow(Base):
    __tablename__ = "scrape_runs"
    __table_args__ = (Index("ix_scrape_runs_source", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    jobs_found: Mapped[int] = mapped_column(default=0)
    jobs_new: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class PlatformSessionRow(Base):
    """Login bookkeeping per platform. Cookies live in the browser profile on
    disk (data/browser-profiles/<platform>); no credentials are stored here."""

    __tablename__ = "platform_sessions"

    platform: Mapped[str] = mapped_column(String(30), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
