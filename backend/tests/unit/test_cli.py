"""CLI error handling: clear messages and exit codes instead of tracebacks."""

from __future__ import annotations

from pathlib import Path

import pytest

import jobpilot.__main__ as cli
from jobpilot.config import Settings, get_settings


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JOBPILOT_PROJECT_ROOT", str(tmp_path))
    for key in ("JOBPILOT_DATA_DIR", "JOBPILOT_PREFERENCES_PATH", "JOBPILOT_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    (tmp_path / "config").mkdir()
    yield tmp_path
    get_settings.cache_clear()


def test_invalid_config_exits_2_with_the_field_name(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "config" / "config.yaml").write_text("max_applications_per_day: 0\n")
    assert cli.main(["config", "show"]) == cli.EXIT_CONFIG_ERROR
    assert "max_applications_per_day" in capsys.readouterr().out


def test_busy_pipeline_exits_3(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from jobpilot.locks import PIPELINE_LOCK, process_lock

    with process_lock(PIPELINE_LOCK, project / "data" / "locks"):
        assert cli.main(["scrape", "--source", "remoteok"]) == cli.EXIT_BUSY
    assert "already in progress" in capsys.readouterr().out


def test_apply_refuses_without_applicant_details(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["apply"]) == cli.EXIT_CONFIG_ERROR
    assert "applicant.name" in capsys.readouterr().out


def test_unknown_source_is_a_clear_error(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scrape", "--source", "monster"]) == 1
    out = capsys.readouterr().out
    assert "Unknown platform 'monster'" in out
    for name in ("remoteok", "linkedin", "jobstreet", "onlinejobs"):
        assert name in out


@pytest.mark.parametrize("argv", [["rank", "--min-score", "150"], ["run", "--top", "0"]])
def test_out_of_range_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_alembic_config_is_found() -> None:
    cfg = cli._alembic_config()
    assert Path(cfg.get_main_option("script_location") or "").is_dir()


def test_prompt_key_hints_are_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug found in the manual E2E run: Rich parsed "[s]", "[r]", ... as style
    tags, so the apply/review prompts showed no keys to press."""
    import asyncio

    from rich.console import Console

    from jobpilot.applications.runner import Decision, FillState
    from jobpilot.domain import Application, Job, JobSource

    recorded = Console(record=True, width=200)
    answers = iter(["k"])
    monkeypatch.setattr(cli, "console", recorded)
    monkeypatch.setattr(
        recorded, "input", lambda prompt="": recorded.print(prompt, end="") or next(answers)
    )
    job = Job(title="T", company="C", application_url="https://x.test", source=JobSource.OTHER)
    state = FillState(result=None, error=None, can_propose=True)
    decision = asyncio.run(cli._ask_decision(Application(id=1, job_id=1, resume_id=1), job, state))
    assert decision is Decision.KEEP
    text = recorded.export_text()
    for hint in (
        "[s] I submitted it",
        "[f] fill",
        "[k] keep",
        "[p] draft answers",
        "[r] reject",
        "[q] quit",
    ):
        assert hint in text


def test_platforms_lists_every_adapter(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["db", "init"]) == 0
    assert cli.main(["platforms"]) == 0
    out = capsys.readouterr().out
    assert "remoteok" in out and "not needed" in out


def test_pause_blocks_new_runs_until_unpaused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["pause", "going", "offline"]) == 0
    assert cli.main(["search"]) == cli.EXIT_PAUSED
    assert cli.main(["run"]) == cli.EXIT_PAUSED
    assert "jobpilot unpause" in capsys.readouterr().out
    assert cli.main(["unpause"]) == 0
    assert not (project / "data" / "PAUSED").exists()


def test_login_for_platform_without_accounts(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["login", "remoteok"]) == 0
    assert "doesn't need an account" in capsys.readouterr().out
    assert cli.main(["login", "myspace"]) == 1


def test_logout_without_saved_profile(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["db", "init"]) == 0
    assert cli.main(["logout", "remoteok", "--yes"]) == 0
    assert "No saved remoteok login" in capsys.readouterr().out
