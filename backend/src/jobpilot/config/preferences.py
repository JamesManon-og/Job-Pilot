"""User-level job-search preferences loaded from config/config.yaml."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class PreferencesError(ValueError):
    """config.yaml is unreadable or invalid. The message says exactly what to fix."""


class RankingWeights(BaseModel):
    """Weights for the composite job ranking (see matcher/ranking.py)."""

    resume_match: float = Field(default=0.55, ge=0)
    salary: float = Field(default=0.10, ge=0)
    remote: float = Field(default=0.10, ge=0)
    recency: float = Field(default=0.10, ge=0)
    tech_overlap: float = Field(default=0.15, ge=0)


_URL_FIELDS = ("portfolio_url", "github_url", "linkedin_url")


class ApplicantProfile(BaseModel):
    """Personal information for autofilling job applications."""

    name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    country: str = ""
    portfolio_url: str = ""
    github_url: str = ""
    linkedin_url: str = ""
    work_authorization: str = ""
    requires_sponsorship: str = ""  # "Yes" / "No"; blank = ask me
    salary_expectation: str = ""
    available_start_date: str = ""
    years_experience: str = ""
    resume_file: str = ""

    @field_validator(*_URL_FIELDS)
    @classmethod
    def _add_url_scheme(cls, value: str) -> str:
        # "github.com/me" fails most forms' URL validation; normalize it.
        value = value.strip()
        if value and "://" not in value:
            return f"https://{value}"
        return value

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip()
        if value and ("@" not in value or " " in value):
            raise ValueError(f"{value!r} doesn't look like an email address")
        return value

    @property
    def first_name(self) -> str:
        parts = self.name.split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def location(self) -> str:
        return ", ".join(part for part in (self.city, self.country) if part)


class UserPreferences(BaseModel):
    """Every knob that controls what gets scraped, matched, and applied to."""

    min_match_score: int = Field(default=75, ge=0, le=100)
    preferred_technologies: list[str] = Field(default_factory=list)
    preferred_countries: list[str] = Field(default_factory=list)
    preferred_salary_min: int | None = Field(default=None, ge=0)
    preferred_job_titles: list[str] = Field(default_factory=list)
    remote_only: bool = True
    blacklist_companies: list[str] = Field(default_factory=list)
    max_applications_per_day: int = Field(default=10, ge=1)
    # Deprecated and ignored: human approval is mandatory and only the human
    # submits. Kept so existing config files still load.
    auto_submit_enabled: bool = False
    human_approval_enabled: bool = True
    resume_version: str = "default"
    cover_letter_template: str = "default"
    ranking_weights: RankingWeights = Field(default_factory=RankingWeights)
    applicant: ApplicantProfile = Field(default_factory=ApplicantProfile)

    def is_company_blacklisted(self, company: str) -> bool:
        normalized = company.strip().lower()
        return any(normalized == entry.strip().lower() for entry in self.blacklist_companies)


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        problems.append(f"  - {location}: {error['msg']}")
    return f"{path} is invalid:\n" + "\n".join(problems)


def load_preferences(path: Path, *, project_root: Path | None = None) -> UserPreferences:
    """Load preferences from YAML; missing file or keys fall back to defaults.

    Relative ``applicant.resume_file`` paths are resolved against `project_root`
    (default: the config file's parent's parent), not the current directory.
    """
    if not path.exists():
        return UserPreferences()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PreferencesError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise PreferencesError(f"Cannot read {path}: {exc}") from exc
    if raw is None:
        return UserPreferences()
    if not isinstance(raw, dict):
        raise PreferencesError(f"Preferences file {path} must contain a YAML mapping")
    try:
        prefs = UserPreferences.model_validate(raw)
    except ValidationError as exc:
        raise PreferencesError(_format_validation_error(path, exc)) from exc

    if prefs.auto_submit_enabled or not prefs.human_approval_enabled:
        logger.warning(
            "auto_submit_enabled / human_approval_enabled are ignored: JobPilot never "
            "submits without your explicit approval and confirmation."
        )
    resume_file = prefs.applicant.resume_file.strip()
    if resume_file:
        resume_path = Path(resume_file).expanduser()
        if not resume_path.is_absolute():
            root = project_root or path.resolve().parent.parent
            resume_path = root / resume_path
        prefs.applicant.resume_file = str(resume_path)
    return prefs
