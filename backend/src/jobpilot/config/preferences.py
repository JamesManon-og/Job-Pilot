"""User-level job-search preferences loaded from config/config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RankingWeights(BaseModel):
    """Weights for the composite job ranking (see matcher/ranking.py)."""

    resume_match: float = Field(default=0.55, ge=0)
    salary: float = Field(default=0.10, ge=0)
    remote: float = Field(default=0.10, ge=0)
    recency: float = Field(default=0.10, ge=0)
    tech_overlap: float = Field(default=0.15, ge=0)


class UserPreferences(BaseModel):
    """Every knob that controls what gets scraped, matched, and applied to."""

    min_match_score: int = Field(default=75, ge=0, le=100)
    preferred_technologies: list[str] = Field(default_factory=list)
    preferred_countries: list[str] = Field(default_factory=list)
    preferred_salary_min: int | None = None
    preferred_job_titles: list[str] = Field(default_factory=list)
    remote_only: bool = True
    blacklist_companies: list[str] = Field(default_factory=list)
    max_applications_per_day: int = Field(default=10, ge=1)
    auto_submit_enabled: bool = False
    human_approval_enabled: bool = True
    resume_version: str = "default"
    cover_letter_template: str = "default"
    ranking_weights: RankingWeights = Field(default_factory=RankingWeights)

    def is_company_blacklisted(self, company: str) -> bool:
        normalized = company.strip().lower()
        return any(normalized == entry.strip().lower() for entry in self.blacklist_companies)


def load_preferences(path: Path) -> UserPreferences:
    """Load preferences from YAML; missing file or keys fall back to defaults."""
    if not path.exists():
        return UserPreferences()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return UserPreferences()
    if not isinstance(raw, dict):
        raise ValueError(f"Preferences file {path} must contain a YAML mapping")
    return UserPreferences.model_validate(raw)
