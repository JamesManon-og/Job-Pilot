"""Environment-level settings loaded from environment variables / .env.

These cover infrastructure concerns (paths, database, Ollama endpoint).
User-facing job-search preferences live in ``preferences.py`` (config.yaml).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    # backend/src/jobpilot/config/settings.py -> project root is 4 levels up
    return Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=_default_project_root)
    database_url: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if not self.database_url:
            db_path = self.data_dir / "jobpilot.db"
            self.database_url = f"sqlite+aiosqlite:///{db_path}"
        return self

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def preferences_path(self) -> Path:
        return self.config_dir / "config.yaml"

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.data_dir / "resumes",
            self.data_dir / "screenshots",
            self.logs_dir,
            self.config_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
