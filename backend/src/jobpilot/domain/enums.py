"""Domain enums. Stored in the database as their string values."""

from __future__ import annotations

from enum import StrEnum


class JobSource(StrEnum):
    REMOTEOK = "remoteok"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WELLFOUND = "wellfound"
    WEWORKREMOTELY = "weworkremotely"
    YCOMBINATOR = "ycombinator"
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    COMPANY_SITE = "company_site"
    OTHER = "other"


class RemoteType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class ExperienceLevel(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    UNKNOWN = "unknown"


class ApplicationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class MatchRecommendation(StrEnum):
    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    MAYBE = "maybe"
    SKIP = "skip"


class ScrapeRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
