from __future__ import annotations

from pathlib import Path

import pytest

from jobpilot.config.preferences import UserPreferences, load_preferences
from jobpilot.config.settings import Settings


class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.database_url.startswith("sqlite+aiosqlite:///")
        assert settings.database_url.endswith("jobpilot.db")
        assert settings.ollama_base_url == "http://localhost:11434"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBPILOT_DATABASE_URL", "sqlite+aiosqlite:///custom.db")
        monkeypatch.setenv("JOBPILOT_LOG_LEVEL", "DEBUG")
        settings = Settings(_env_file=None)
        assert settings.database_url == "sqlite+aiosqlite:///custom.db"
        assert settings.log_level == "DEBUG"

    def test_derived_paths(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, project_root=tmp_path)
        assert settings.data_dir == tmp_path / "data"
        assert settings.preferences_path == tmp_path / "config" / "config.yaml"


class TestPreferences:
    def test_defaults_when_file_missing(self, tmp_path: Path) -> None:
        prefs = load_preferences(tmp_path / "missing.yaml")
        assert prefs == UserPreferences()
        assert prefs.human_approval_enabled is True
        assert prefs.auto_submit_enabled is False

    def test_partial_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("min_match_score: 80\nblacklist_companies: [Acme Corp]\n")
        prefs = load_preferences(path)
        assert prefs.min_match_score == 80
        assert prefs.max_applications_per_day == 10  # default preserved

    def test_blacklist_is_case_insensitive(self) -> None:
        prefs = UserPreferences(blacklist_companies=["Acme Corp"])
        assert prefs.is_company_blacklisted("acme corp")
        assert prefs.is_company_blacklisted("  ACME CORP  ")
        assert not prefs.is_company_blacklisted("Other Inc")

    def test_invalid_score_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("min_match_score: 150\n")
        with pytest.raises(ValueError):
            load_preferences(path)

    def test_non_mapping_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError):
            load_preferences(path)
