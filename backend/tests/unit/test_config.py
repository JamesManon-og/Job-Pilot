from __future__ import annotations

from pathlib import Path

import pytest

from jobpilot.config.preferences import PreferencesError, UserPreferences, load_preferences
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


class TestSettingsOverrides:
    def test_data_dir_and_preferences_path_env_vars_are_honored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Bug: README and Dockerfile set JOBPILOT_DATA_DIR / JOBPILOT_PREFERENCES_PATH
        but Settings ignored them — the container wrote its DB outside the volume."""
        monkeypatch.setenv("JOBPILOT_DATA_DIR", str(tmp_path / "vol"))
        monkeypatch.setenv("JOBPILOT_PREFERENCES_PATH", str(tmp_path / "cfg" / "c.yaml"))
        settings = Settings(_env_file=None)
        assert settings.data_dir == tmp_path / "vol"
        assert settings.database_url.endswith(str(tmp_path / "vol" / "jobpilot.db"))
        assert settings.preferences_path == tmp_path / "cfg" / "c.yaml"
        assert settings.locks_dir == tmp_path / "vol" / "locks"

    def test_env_files_are_absolute_not_cwd_relative(self) -> None:
        """Bug: env_file=".env" silently ignored backend/.env outside backend/."""
        env_files = Settings.model_config["env_file"]
        assert isinstance(env_files, tuple)
        assert all(Path(p).is_absolute() for p in env_files)

    def test_invalid_log_level_is_a_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBPILOT_LOG_LEVEL", "LOUD")
        with pytest.raises(ValueError, match="JOBPILOT_LOG_LEVEL"):
            Settings(_env_file=None)


class TestPreferencesValidation:
    def test_errors_name_the_field_and_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("max_applications_per_day: 0\nmin_match_score: -5\n")
        with pytest.raises(PreferencesError) as excinfo:
            load_preferences(path)
        message = str(excinfo.value)
        assert str(path) in message
        assert "max_applications_per_day" in message and "min_match_score" in message

    def test_broken_yaml_is_a_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("applicant:\n  name: [unclosed\n")
        with pytest.raises(PreferencesError, match="not valid YAML"):
            load_preferences(path)

    def test_relative_resume_path_resolves_against_project_root(self, tmp_path: Path) -> None:
        """Bug: "data/resumes/cv.pdf" resolved against the current directory, so
        running from backend/ silently skipped the upload."""
        (tmp_path / "config").mkdir()
        path = tmp_path / "config" / "config.yaml"
        path.write_text("applicant:\n  resume_file: data/resumes/cv.pdf\n")
        prefs = load_preferences(path)
        assert prefs.applicant.resume_file == str(tmp_path / "data" / "resumes" / "cv.pdf")

    def test_profile_urls_get_a_scheme(self) -> None:
        prefs = UserPreferences.model_validate(
            {"applicant": {"github_url": "github.com/me", "linkedin_url": "https://x.y/z"}}
        )
        assert prefs.applicant.github_url == "https://github.com/me"
        assert prefs.applicant.linkedin_url == "https://x.y/z"

    def test_obviously_bad_email_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="email"):
            UserPreferences.model_validate({"applicant": {"email": "not an email"}})

    def test_name_parts(self) -> None:
        from jobpilot.config.preferences import ApplicantProfile

        assert ApplicantProfile(name="Maria de la Cruz").last_name == "de la Cruz"
        assert ApplicantProfile(name="Cher").last_name == ""
        assert ApplicantProfile(city="Cebu", country="Philippines").location == "Cebu, Philippines"
