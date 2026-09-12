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
    JOBSTREET = "jobstreet"
    ONLINEJOBS = "onlinejobs"
    COMPANY_SITE = "company_site"
    OTHER = "other"


class JobStatus(StrEnum):
    """Where a job is in the pipeline. Review sub-states live on the Application."""

    DISCOVERED = "discovered"  # collected, not scored yet
    MATCHED = "matched"  # scored against the active resume
    PREPARED = "prepared"  # has an application in review / approved / open
    APPLIED = "applied"  # application submitted (confirmed by the user)
    REJECTED = "rejected"  # the user rejected its application
    SKIPPED = "skipped"  # duplicate of another listing, or excluded — never processed


class SessionStatus(StrEnum):
    """State of a platform's saved browser login."""

    NOT_REQUIRED = "not_required"  # public API / no account needed
    UNKNOWN = "unknown"  # never checked
    LOGGED_IN = "logged_in"
    NEEDS_LOGIN = "needs_login"  # logged out or expired: run `jobpilot login <platform>`
    BLOCKED = "blocked"  # CAPTCHA / checkpoint / bot wall: needs the human


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
    # Form opened and autofilled in a visible browser; waiting for the human to
    # submit it themselves and confirm. Counts toward the daily cap.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
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
