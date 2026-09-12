"""Environment-level settings loaded from environment variables / .env.

These cover infrastructure concerns (paths, database, Ollama endpoint).
User-facing job-search preferences live in ``preferences.py`` (config.yaml).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    # backend/src/jobpilot/config/settings.py -> project root is 4 levels up
    return Path(__file__).resolve().parents[4]


_SOURCE_ROOT = _default_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBPILOT_",
        # Absolute paths: loading ".env" relative to the current directory
        # silently ignored backend/.env whenever commands ran from elsewhere.
        env_file=(_SOURCE_ROOT / ".env", _SOURCE_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=_default_project_root)
    # Each defaults to a location under project_root; override via
    # JOBPILOT_DATA_DIR / JOBPILOT_LOGS_DIR / JOBPILOT_PREFERENCES_PATH.
    data_dir: Path = Field(default_factory=lambda data: data["project_root"] / "data")
    logs_dir: Path = Field(default_factory=lambda data: data["project_root"] / "logs")
    preferences_path: Path = Field(
        default_factory=lambda data: data["project_root"] / "config" / "config.yaml"
    )
    database_url: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if not self.database_url:
            db_path = self.data_dir / "jobpilot.db"
            self.database_url = f"sqlite+aiosqlite:///{db_path}"
        self.log_level = self.log_level.upper()
        return self

    @model_validator(mode="before")
    @classmethod
    def _validate_log_level(cls, data: Any) -> Any:
        level = data.get("log_level") if isinstance(data, dict) else None
        if isinstance(level, str) and level.upper() not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError(
                f"JOBPILOT_LOG_LEVEL={level!r} is not a log level "
                "(use DEBUG, INFO, WARNING, ERROR, or CRITICAL)"
            )
        return data

    @property
    def config_dir(self) -> Path:
        return self.preferences_path.parent

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def backend_dir(self) -> Path:
        return self.project_root / "backend"

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.data_dir / "resumes",
            self.screenshots_dir,
            self.locks_dir,
            self.logs_dir,
            self.config_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
