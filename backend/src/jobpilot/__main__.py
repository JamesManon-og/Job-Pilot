"""JobPilot CLI: `python -m jobpilot <command>`.

Run `python -m jobpilot --help` for the command list. Batch commands that
write jobs/matches/applications (scrape, match, prepare, run) share one
cross-process lock, so two of them can never race each other.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from jobpilot.applications.runner import Decision, StaleDecision
    from jobpilot.autofill import FilledForm
    from jobpilot.config import Settings, UserPreferences
    from jobpilot.domain.enums import JobSource, SessionStatus
    from jobpilot.domain.models import Application, Job, PlatformSession, ScrapeRun
    from jobpilot.llm import OllamaClient
    from jobpilot.matcher import MatchService
    from jobpilot.platforms import BrowserProfiles

from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jobpilot.config import PreferencesError, get_settings, load_user_preferences
from jobpilot.control import AgentPausedError
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.locks import PIPELINE_LOCK, LockBusyError, process_lock
from jobpilot.logging_setup import setup_logging
from jobpilot.platforms.browser import ProfileInUseError

logger = logging.getLogger(__name__)
console = Console()

EXIT_CONFIG_ERROR = 2
EXIT_BUSY = 3
EXIT_PAUSED = 4


def _alembic_config() -> AlembicConfig:
    # Editable installs run from backend/src; installed copies (Docker) find
    # the migrations via the project root instead.
    candidates = [Path(__file__).resolve().parents[2], get_settings().backend_dir]
    for backend_dir in candidates:
        if (backend_dir / "alembic.ini").is_file():
            cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
            cfg.set_main_option("script_location", str(backend_dir / "migrations"))
            return cfg
    raise FileNotFoundError(
        "Cannot find alembic.ini. Looked in: "
        + ", ".join(str(c) for c in candidates)
        + ". Set JOBPILOT_PROJECT_ROOT to the directory containing backend/."
    )


def _pipeline_lock() -> AbstractContextManager[None]:
    return process_lock(PIPELINE_LOCK, get_settings().locks_dir)


def _prefs() -> UserPreferences:
    return load_user_preferences(get_settings())


def cmd_db_init() -> int:
    settings = get_settings()
    settings.ensure_directories()
    command.upgrade(_alembic_config(), "head")
    console.print(f"[green]Database ready:[/green] {settings.database_url}")
    return 0


async def _collect_stats() -> dict[str, int]:
    from sqlalchemy import func, select

    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.orm import Base

    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    stats: dict[str, int] = {}
    try:
        async with session_factory() as session:
            for table in Base.metadata.sorted_tables:
                count = await session.scalar(select(func.count()).select_from(table))
                stats[table.name] = count or 0
    finally:
        await engine.dispose()
    return stats


def cmd_db_stats() -> int:
    stats = asyncio.run(_collect_stats())
    table = Table(title="JobPilot database")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for name, count in stats.items():
        table.add_row(name, str(count))
    console.print(table)
    return 0


def _profiles(*, headless: bool = True) -> BrowserProfiles:
    from jobpilot.platforms import BrowserProfiles

    settings = get_settings()
    return BrowserProfiles(
        settings.profiles_dir,
        settings.locks_dir,
        headless=headless,
        channel=settings.browser_channel,
    )


def _resolve_platforms(name: str, preferences: UserPreferences) -> list[JobSource] | None:
    """'all' = every platform enabled in config.yaml; a name = that one, even if disabled."""
    from jobpilot.domain.enums import JobSource
    from jobpilot.platforms import all_adapter_classes, enabled_platforms

    registered = [p.value for p in all_adapter_classes()]
    if name == "all":
        return enabled_platforms(preferences)
    if name not in registered:
        console.print(
            f"[red]Unknown platform {name!r}.[/red] Available: {', '.join(registered)} (or 'all')"
        )
        return None
    return [JobSource(name)]


def _ensure_not_paused() -> None:
    from jobpilot.control import ensure_running

    ensure_running(get_settings().data_dir)


async def _run_search(platform: str, headed: bool) -> int:
    from jobpilot.control import is_paused
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.platforms import SearchRunner

    settings = get_settings()
    preferences = _prefs()
    _ensure_not_paused()
    targets = _resolve_platforms(platform, preferences)
    if targets is None:
        return 1
    if not targets:
        console.print(
            "[yellow]No platforms enabled.[/yellow] Enable some under `platforms:` in "
            f"{settings.preferences_path} (see `jobpilot platforms`)."
        )
        return 1
    engine = create_engine(settings.database_url)
    runner = SearchRunner(
        create_session_factory(engine),
        preferences,
        profiles=_profiles(headless=not headed),
        paused=lambda: is_paused(settings.data_dir),
    )
    results: list[ScrapeRun] = []
    try:
        with _pipeline_lock():
            for target in targets:
                console.print(f"Searching {target.value}…")
                results.append(await runner.run(target))
    finally:
        await engine.dispose()

    table = Table(title="Search results")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Found", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Known", justify="right")
    table.add_column("Filtered", justify="right")
    table.add_column("Error")
    for run in results:
        stats = runner.last_stats.get(run.source)
        table.add_row(
            run.source.value,
            run.status.value,
            str(run.jobs_found),
            str(run.jobs_new),
            str(stats.known if stats else ""),
            str(stats.filtered if stats else ""),
            run.error or "",
        )
    console.print(table)
    return 0 if all(run.error is None for run in results) else 1


async def _session_rows() -> dict[str, PlatformSession]:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import PlatformSessionRepository

    engine = create_engine(get_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            return {s.platform.value: s for s in await PlatformSessionRepository(session).list()}
    finally:
        await engine.dispose()


async def _record_session(platform: JobSource, status: SessionStatus, detail: str = "") -> None:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import PlatformSessionRepository

    engine = create_engine(get_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await PlatformSessionRepository(session).record(platform, status, detail=detail)
            await session.commit()
    finally:
        await engine.dispose()


async def _run_platforms() -> int:
    from jobpilot.platforms import all_adapter_classes

    preferences = _prefs()
    sessions = await _session_rows()
    profiles = _profiles()
    table = Table(title="Platforms")
    table.add_column("Platform")
    table.add_column("Enabled")
    table.add_column("Account")
    table.add_column("Session")
    table.add_column("Checked")
    for platform, cls in all_adapter_classes().items():
        saved = sessions.get(platform.value)
        if cls.login_url is None:
            account, session_text = "not needed", "—"
        else:
            account = "saved profile" if profiles.has_profile(platform) else "not logged in"
            session_text = saved.status.value if saved else "unknown"
        table.add_row(
            f"{cls.display_name} ({platform.value})",
            "✓" if preferences.platform(platform.value).enabled else "",
            account,
            session_text,
            saved.last_checked_at.strftime("%Y-%m-%d %H:%M")
            if saved and saved.last_checked_at
            else "",
        )
    console.print(table)
    console.print(
        "[dim]Log in once with `jobpilot login <platform>`, then set "
        "platforms.<platform>.enabled: true in config.yaml.[/dim]"
    )
    return 0


async def _run_login(name: str, timeout_minutes: int) -> int:
    import time

    from jobpilot.domain.enums import JobSource, SessionStatus
    from jobpilot.platforms import all_adapter_classes

    classes = {p.value: cls for p, cls in all_adapter_classes().items()}
    if name not in classes:
        console.print(f"[red]Unknown platform {name!r}.[/red] Available: {', '.join(classes)}")
        return 1
    cls = classes[name]
    platform = JobSource(name)
    if cls.login_url is None:
        console.print(f"{cls.display_name} doesn't need an account.")
        return 0
    adapter = cls(_prefs().platform(name))
    console.print(
        f"Opening {cls.display_name} in a browser window. [bold]Log in yourself[/bold] — "
        "JobPilot never sees, types, or stores your password. Complete any CAPTCHA or "
        "verification code yourself. The window closes once you're logged in "
        f"(or after {timeout_minutes} min)."
    )
    async with _profiles(headless=False).open(platform) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.goto(cls.login_url)
        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline and not page.is_closed():
            try:
                if await adapter.looks_logged_in(browser, page):
                    break
            except Exception:  # noqa: BLE001 - the human is mid-navigation; just poll again
                pass
            await asyncio.sleep(2)
    # Verify from a fresh headless session: proves the cookies were saved.
    async with _profiles().open(platform) as browser:
        status = await adapter.session_status(browser)
    await _record_session(platform, status)
    if status is SessionStatus.LOGGED_IN:
        console.print(
            f"[green]✓ Logged in to {cls.display_name}.[/green] Saved to "
            f"{_profiles().profile_path(platform)} (owner-only permissions)."
        )
        return 0
    console.print(
        f"[yellow]{cls.display_name} session: {status.value}.[/yellow] Run "
        f"`jobpilot login {name}` again when you're ready."
    )
    return 1


async def _run_logout(name: str, assume_yes: bool) -> int:
    from jobpilot.domain.enums import JobSource, SessionStatus
    from jobpilot.platforms import all_adapter_classes

    if name not in {p.value for p in all_adapter_classes()}:
        console.print(f"[red]Unknown platform {name!r}.[/red]")
        return 1
    platform = JobSource(name)
    profiles = _profiles()
    if not profiles.has_profile(platform):
        console.print(f"No saved {name} login.")
        return 0
    if not assume_yes:
        answer = await asyncio.to_thread(
            console.input,
            escape(
                f"Delete JobPilot's saved {name} login ({profiles.profile_path(platform)})? "
                "Your account itself is not affected. [y/N] > "
            ),
        )
        if answer.strip().lower() != "y":
            console.print("Kept.")
            return 0
    profiles.delete_profile(platform)
    await _record_session(platform, SessionStatus.NEEDS_LOGIN, "logged out by user")
    console.print(f"Deleted the saved {name} login.")
    return 0


async def _run_status() -> int:
    from jobpilot.applications import local_day_start
    from jobpilot.control import is_paused, pause_reason
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import (
        ApplicationRepository,
        JobRepository,
        ScrapeRunRepository,
    )

    settings = get_settings()
    preferences = _prefs()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            jobs = await JobRepository(session).count_by_status()
            apps = ApplicationRepository(session)
            applications = {
                status.value: len(await apps.list(status=status, limit=10_000))
                for status in ApplicationStatus
            }
            used_today = await apps.count_toward_cap(local_day_start())
            runs = await ScrapeRunRepository(session).list_recent(limit=20)
    finally:
        await engine.dispose()
    sessions = await _session_rows()

    if is_paused(settings.data_dir):
        console.print(f"[bold yellow]PAUSED[/bold yellow] {pause_reason(settings.data_dir)}")
    funnel = Table(title="Pipeline")
    funnel.add_column("Jobs")
    funnel.add_column("", justify="right")
    for status in ("discovered", "matched", "prepared", "applied", "rejected", "skipped"):
        funnel.add_row(status, str(jobs.get(status, 0)))
    console.print(funnel)
    app_table = Table(title="Applications")
    app_table.add_column("Status")
    app_table.add_column("", justify="right")
    for status, count in applications.items():
        if count:
            app_table.add_row(status, str(count))
    console.print(app_table)
    console.print(
        f"Today: {used_today}/{preferences.max_applications_per_day} submitted or open "
        "for submission"
    )
    latest: dict[str, ScrapeRun] = {}
    for run in runs:
        latest.setdefault(run.source.value, run)
    for name, run in latest.items():
        session_state = sessions.get(name)
        extra = f" · session {session_state.status.value}" if session_state else ""
        console.print(
            f"  {name}: last search {run.status.value}, {run.jobs_new} new{extra}"
            + (f" — [red]{run.error}[/red]" if run.error else "")
        )
    return 0


def cmd_pause(reason: str) -> int:
    from jobpilot.control import pause

    pause(get_settings().data_dir, reason)
    console.print(
        "[yellow]Paused.[/yellow] Running searches stop at the next listing; nothing new "
        "starts until `jobpilot unpause`."
    )
    return 0


def cmd_unpause() -> int:
    from jobpilot.control import resume

    console.print("Resumed." if resume(get_settings().data_dir) else "Wasn't paused.")
    return 0


async def _configured_llm() -> OllamaClient | None:
    """Return the configured Ollama client if the server and model are ready."""
    from jobpilot.llm import OllamaClient

    settings = get_settings()
    client = OllamaClient(settings.ollama_base_url, settings.ollama_model)
    if await client.is_available() and await client.has_model():
        return client
    return None


async def _run_llm_check() -> int:
    from jobpilot.llm import OllamaClient, OllamaError

    settings = get_settings()
    client = OllamaClient(settings.ollama_base_url, settings.ollama_model)

    if not await client.is_available():
        console.print(f"[red]✗[/red] Ollama server unreachable at {settings.ollama_base_url}")
        console.print("  Start it with: [bold]brew services start ollama[/bold]")
        return 1
    console.print(f"[green]✓[/green] Ollama server up at {settings.ollama_base_url}")

    if not await client.has_model():
        console.print(f"[red]✗[/red] Model {settings.ollama_model} not found")
        console.print(f"  Pull it with: [bold]ollama pull {settings.ollama_model}[/bold]")
        return 1
    console.print(f"[green]✓[/green] Model {settings.ollama_model} available")

    try:
        reply = await client.generate("Reply with the single word: ready")
    except OllamaError as exc:
        console.print(f"[red]✗[/red] Generation failed: {exc}")
        return 1
    console.print(f"[green]✓[/green] Generation works (model said: {reply.strip()[:60]!r})")
    return 0


async def _run_resume_import(path: Path, version: str, activate: bool) -> int:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.resume import import_resume

    settings = get_settings()
    engine = create_engine(settings.database_url)
    llm = await _configured_llm()
    if llm is not None:
        console.print(f"[dim]Using {llm.model} to refine the parse…[/dim]")
    try:
        resume = await import_resume(
            create_session_factory(engine), path, version=version, activate=activate, llm=llm
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    finally:
        await engine.dispose()

    profile = resume.profile
    console.print(f"[green]Imported[/green] {path.name} as version [bold]{version}[/bold]")
    console.print(f"  Technologies: {', '.join(profile.technologies) or '(none found)'}")
    console.print(f"  Skills: {len(profile.skills)}  Projects: {len(profile.projects)}")
    console.print(f"  Years experience: {profile.years_experience or 'unknown'}")
    console.print(f"  Active: {resume.is_active}")
    return 0


async def _run_resume_list() -> int:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import ResumeRepository

    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            resumes = await ResumeRepository(session).list()
    finally:
        await engine.dispose()

    table = Table(title="Resumes")
    table.add_column("ID", justify="right")
    table.add_column("Version")
    table.add_column("Active")
    table.add_column("File")
    table.add_column("Technologies", justify="right")
    for resume in resumes:
        table.add_row(
            str(resume.id),
            resume.version,
            "✓" if resume.is_active else "",
            Path(resume.file_path).name,
            str(len(resume.profile.technologies)),
        )
    console.print(table)
    return 0


async def _match_service() -> tuple[MatchService, AsyncEngine] | None:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.matcher import MatchEngine, MatchService

    settings = get_settings()
    preferences = _prefs()
    llm = await _configured_llm()
    if llm is None:
        console.print(
            "[red]LLM not ready.[/red] Run [bold]jobpilot llm check[/bold] for diagnostics."
        )
        return None
    engine = create_engine(settings.database_url)
    service = MatchService(
        create_session_factory(engine), MatchEngine(llm, model_name=llm.model), preferences
    )
    return service, engine


async def _run_match(job_id: int | None, limit: int) -> int:
    from jobpilot.llm import OllamaError
    from jobpilot.matcher import MatchingAbortedError, NoActiveResumeError

    built = await _match_service()
    if built is None:
        return 1
    service, engine = built
    try:
        with _pipeline_lock():
            if job_id is not None:
                results = [await service.match_one(job_id)]
            else:
                results = await service.match_unscored(limit=limit)
    except MatchingAbortedError as exc:
        console.print(f"[red]Matching stopped:[/red] {exc}")
        console.print(f"[dim]{len(exc.scored)} job(s) scored before that were saved.[/dim]")
        return 1
    except (NoActiveResumeError, ValueError, OllamaError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    finally:
        await engine.dispose()

    console.print(f"[green]Scored {len(results)} job(s).[/green]")
    if service.last_failures:
        console.print(
            f"[yellow]{len(service.last_failures)} job(s) could not be scored and will be "
            "retried next run (see logs).[/yellow]"
        )
    return 0


async def _run_rank(top: int, min_score: int) -> int:
    from jobpilot.matcher import NoActiveResumeError

    built = await _match_service()
    if built is None:
        return 1
    service, engine = built
    try:
        ranked = await service.ranked(min_score=min_score, top=top)
    except NoActiveResumeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    finally:
        await engine.dispose()

    table = Table(title=f"Top {len(ranked)} matches")
    table.add_column("Job", justify="right")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("LLM", justify="right")
    table.add_column("Composite", justify="right")
    table.add_column("Recommendation")
    table.add_column("Missing")
    for entry in ranked:
        table.add_row(
            str(entry.job.id),
            entry.job.title[:40],
            entry.job.company[:24],
            str(entry.match.score),
            f"{entry.composite_score:.2f}",
            entry.match.recommendation.value,
            ", ".join(entry.match.missing_skills[:3]),
        )
    console.print(table)
    return 0


async def _run_prepare(job_id: int) -> int:
    from jobpilot.applications import (
        ApplicationService,
        BlacklistedCompanyError,
        DailyCapReachedError,
    )
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import DuplicateApplicationError, ResumeRepository

    settings = get_settings()
    preferences = _prefs()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            resume = await ResumeRepository(session).get_active()
        if resume is None or resume.id is None:
            console.print("[red]No active resume.[/red] Run: jobpilot resume import <path>")
            return 1
        llm = await _configured_llm()
        if llm is None:
            console.print("[yellow]LLM unavailable — preparing without a cover letter.[/yellow]")
        service = ApplicationService(factory, preferences, llm=llm)
        with _pipeline_lock():
            application = await service.prepare(job_id, resume_id=resume.id)
    except (DuplicateApplicationError, DailyCapReachedError, BlacklistedCompanyError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return 1
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    finally:
        await engine.dispose()

    console.print(
        f"[green]Application #{application.id} prepared[/green] "
        f"(status: {application.status.value})"
    )
    if application.notes:
        console.print(f"[yellow]{application.notes}[/yellow]")
    console.print("Review it with [bold]jobpilot review[/bold] or in the dashboard.")
    return 0


async def _run_pipeline(platform: str, top: int, min_score: int, headed: bool) -> int:
    """Full pipeline: search → match → rank → prepare applications for top matches."""
    from jobpilot.control import is_paused
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.pipeline import PipelineService
    from jobpilot.platforms import SearchRunner

    settings = get_settings()
    preferences = _prefs()
    _ensure_not_paused()
    targets = _resolve_platforms(platform, preferences)
    if targets is None:
        return 1
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    llm = await _configured_llm()
    search = SearchRunner(
        factory,
        preferences,
        profiles=_profiles(headless=not headed),
        paused=lambda: is_paused(settings.data_dir),
    )
    pipeline = PipelineService(
        factory, preferences, llm=llm, model_name=llm.model if llm else "", search=search
    )
    threshold = max(preferences.min_match_score, min_score)
    names = ", ".join(t.value for t in targets) or "no platforms enabled"
    console.print(
        f"[bold]Pipeline[/bold]: search {names} → match → rank "
        f"(LLM score ≥ {threshold}) → prepare top {top}"
    )
    try:
        with _pipeline_lock():
            report = await pipeline.run(targets, top=top, min_score=min_score)
    finally:
        await engine.dispose()

    for run in report.scrape_runs:
        status = "[green]ok[/green]" if run.error is None else f"[red]failed: {run.error}[/red]"
        console.print(
            f"  scrape {run.source.value}: {run.jobs_new} new / {run.jobs_found} found — {status}"
        )
    console.print(f"  scored {report.scored} job(s)")
    if report.score_failures:
        console.print(
            f"  [yellow]{report.score_failures} job(s) failed to score (retried next run)[/yellow]"
        )
    for reason in report.skipped:
        console.print(f"  [dim]skipped {reason}[/dim]")
    for app_id in report.prepared:
        console.print(f"  [green]prepared application #{app_id}[/green]")
    if report.stopped_early:
        console.print(f"[yellow]{report.stopped_early}[/yellow]")
    elif not report.candidates:
        console.print(f"  No unapplied matches with LLM score ≥ {threshold}.")
    console.print(
        f"\n[bold green]Pipeline complete.[/bold green] {len(report.prepared)} application(s) "
        "prepared."
    )
    if report.prepared:
        console.print(
            "Next: [bold]jobpilot review[/bold] (or the dashboard), then "
            "[bold]jobpilot apply[/bold]."
        )
    failed_scrapes = any(run.error for run in report.scrape_runs)
    return 1 if failed_scrapes and not report.prepared else 0


def _edit_in_editor(text: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="jobpilot-cover-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        subprocess.run([*editor.split(), name], check=False)
        return Path(name).read_text(encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)


async def _run_review() -> int:
    """Interactive terminal review of pending applications."""
    from jobpilot.applications import ApplicationService, InvalidTransitionError
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import ApplicationRepository, JobRepository
    from jobpilot.domain.enums import ApplicationStatus

    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    service = ApplicationService(factory, _prefs())
    try:
        async with factory() as session:
            pending = list(
                reversed(
                    await ApplicationRepository(session).list(
                        status=ApplicationStatus.PENDING_REVIEW, limit=500
                    )
                )
            )
            jobs = {a.job_id: await JobRepository(session).get(a.job_id) for a in pending}
        if not pending:
            console.print("Nothing to review.")
            return 0
        for application in pending:
            assert application.id is not None
            job = jobs[application.job_id]
            while True:
                console.rule(f"Application #{application.id}")
                if job is not None:
                    console.print(f"[bold]{job.title}[/bold] @ {job.company}  ({job.source.value})")
                    console.print(f"[link={job.application_url}]{job.application_url}[/link]")
                if application.notes:
                    console.print(f"[yellow]{application.notes}[/yellow]")
                console.print("\n[bold]Cover letter[/bold]")
                console.print(application.cover_letter or "[dim](empty)[/dim]")
                for question, answer in application.answers.items():
                    console.print(f"\n[bold]{question}[/bold]\n{answer}")
                choice = (
                    (
                        await asyncio.to_thread(
                            console.input,
                            escape("\n[a]pprove  [r]eject  [e]dit cover letter  [s]kip  [q]uit > "),
                        )
                    )
                    .strip()
                    .lower()
                )
                try:
                    if choice == "a":
                        await service.approve(application.id)
                        console.print("[green]Approved.[/green]")
                    elif choice == "r":
                        await service.reject(application.id)
                        console.print("Rejected.")
                    elif choice == "e":
                        edited = await asyncio.to_thread(_edit_in_editor, application.cover_letter)
                        async with factory() as session:
                            application = await ApplicationRepository(session).update_materials(
                                application.id, cover_letter=edited
                            )
                            await session.commit()
                        continue
                    elif choice == "q":
                        return 0
                    elif choice != "s":
                        continue
                except InvalidTransitionError as exc:
                    console.print(f"[yellow]{exc} (changed elsewhere)[/yellow]")
                break
        console.print("Done. Run [bold]jobpilot apply[/bold] to open approved applications.")
        return 0
    finally:
        await engine.dispose()


def _print_fill_summary(result: FilledForm | None, error: str | None) -> None:
    if error:
        console.print(f"[red]Autofill problem:[/red] {error}")
    if result is None:
        return
    if result.blocker:
        console.print(
            f"[bold yellow]This page is a {result.blocker} wall.[/bold yellow] JobPilot won't "
            "touch it. Handle it yourself in the browser, then choose \\[f] to fill again."
        )
        return
    if result.fields_filled:
        console.print("[green]Filled:[/green] " + ", ".join(sorted(result.fields_filled)))
    if result.fields_skipped:
        console.print("[yellow]Answer these yourself:[/yellow]")
        for label in result.fields_skipped:
            console.print(f"  • {label}")
    if result.sensitive_skipped:
        console.print(
            "[yellow]Sensitive — left for you:[/yellow] " + ", ".join(result.sensitive_skipped)
        )
    if result.required_unfilled:
        console.print("[bold red]Still required before submitting:[/bold red]")
        for label in result.required_unfilled:
            console.print(f"  • {label}")
    if result.resume_problem:
        console.print(f"[yellow]Resume not attached:[/yellow] {result.resume_problem}")
    if result.captcha_present:
        console.print("[yellow]There's a CAPTCHA — complete it yourself.[/yellow]")
    if result.screenshot_path:
        console.print(f"[dim]Screenshot: {result.screenshot_path}[/dim]")


async def _ask_decision(
    application: Application, job: Job, result: FilledForm | None, error: str | None
) -> Decision:
    from jobpilot.applications.runner import Decision

    console.rule(f"#{application.id} {job.title} @ {job.company}")
    _print_fill_summary(result, error)
    console.print(
        "\nCheck every field in the browser window. [bold]Submit it yourself[/bold] on the "
        "site when you're satisfied — JobPilot never clicks submit."
    )
    choices = {
        "s": Decision.SUBMITTED,
        "f": Decision.REFILL,
        "k": Decision.KEEP,
        "r": Decision.REJECT,
        "q": Decision.QUIT,
    }
    while True:
        choice = (
            (
                await asyncio.to_thread(
                    console.input,
                    # escape(): Rich would read "[s]" etc. as style tags and hide them.
                    escape(
                        "[s] I submitted it  [f] fill this page again  [k] keep for later  "
                        "[r] reject  [q] quit > "
                    ),
                )
            )
            .strip()
            .lower()
        )
        if choice == "s":
            confirm = await asyncio.to_thread(
                console.input, escape("Confirm you clicked the site's submit button [y/N] > ")
            )
            if confirm.strip().lower() != "y":
                continue
        if choice in choices:
            return choices[choice]


async def _ask_stale(application: Application, job: Job) -> StaleDecision:
    from jobpilot.applications.runner import StaleDecision

    console.print(
        f"[yellow]Application #{application.id} ({job.title} @ {job.company}) was open in a "
        "browser when a previous `jobpilot apply` ended.[/yellow]"
    )
    while True:
        choice = (
            (
                await asyncio.to_thread(
                    console.input,
                    escape("Did you submit it? [y] yes  [n] no, keep it approved  [r] reject > "),
                )
            )
            .strip()
            .lower()
        )
        if choice == "y":
            return StaleDecision.SUBMITTED
        if choice == "n":
            return StaleDecision.NOT_SUBMITTED
        if choice == "r":
            return StaleDecision.REJECT


async def _run_apply(application_ids: list[int]) -> int:
    from playwright.async_api import async_playwright

    from jobpilot.applications import ApplicationService, missing_applicant_fields
    from jobpilot.applications.runner import ApplyRunner
    from jobpilot.autofill import AutofillEngine
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import (
        ApplicationRepository,
        JobRepository,
        ResumeRepository,
    )
    from jobpilot.domain.enums import ApplicationStatus

    settings = get_settings()
    preferences = _prefs()
    missing = missing_applicant_fields(preferences.applicant)
    if missing:
        console.print(
            f"[red]Missing applicant info:[/red] set applicant.{', applicant.'.join(missing)} "
            f"in {settings.preferences_path} first."
        )
        return EXIT_CONFIG_ERROR
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        with process_lock("apply", settings.locks_dir):
            async with factory() as session:
                apps = ApplicationRepository(session)
                jobs = JobRepository(session)
                resume = await ResumeRepository(session).get_active()
                stale = [
                    (a, job)
                    for a in await apps.list(status=ApplicationStatus.AWAITING_CONFIRMATION)
                    if (job := await jobs.get(a.job_id)) is not None
                ]
                if not application_ids:
                    approved = await apps.list(status=ApplicationStatus.APPROVED, limit=500)
                    application_ids = [a.id for a in reversed(approved) if a.id is not None]

            service = ApplicationService(factory, preferences)
            autofill = AutofillEngine(
                preferences.applicant,
                settings.screenshots_dir,
                headless=False,
                resume_file=resume.file_path if resume else None,
            )
            runner = ApplyRunner(service, autofill, ask=_ask_decision, say=console.print)
            if stale:
                await runner.resolve_stale(stale, _ask_stale)
            if not application_ids:
                console.print(
                    "No approved applications. Approve some with [bold]jobpilot review[/bold] "
                    "or in the dashboard. (To prepare one for a job: jobpilot prepare <job_id>)"
                )
                return 0

            console.print(f"Opening {len(application_ids)} approved application(s)…")
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False)
                try:
                    context = await browser.new_context(no_viewport=True)
                    for app_id in application_ids:
                        outcome = await runner.process(context, app_id)
                        console.print(f"#{app_id}: {outcome.detail}")
                        if outcome.stop:
                            break
                finally:
                    await browser.close()
    finally:
        await engine.dispose()
    return 0


def cmd_config_show() -> int:
    settings = get_settings()
    preferences = _prefs()
    console.print("[bold]Settings[/bold]")
    console.print_json(settings.model_dump_json())
    console.print("[bold]Preferences[/bold]")
    console.print_json(preferences.model_dump_json())
    from jobpilot.applications import missing_applicant_fields

    missing = missing_applicant_fields(preferences.applicant)
    if missing:
        console.print(
            f"[yellow]Applying needs applicant.{', applicant.'.join(missing)} — "
            f"edit {settings.preferences_path}[/yellow]"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("db", help="database management")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="create/upgrade the database schema")
    db_sub.add_parser("stats", help="print row counts per table")

    config_parser = subparsers.add_parser("config", help="configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="print resolved settings and preferences")

    for name, help_text in (
        ("search", "search enabled job platforms"),
        ("scrape", "alias of search"),
    ):
        search_parser = subparsers.add_parser(name, help=help_text)
        search_parser.add_argument(
            "--platform",
            "--source",
            dest="platform",
            default="all",
            help="platform name (e.g. linkedin) or 'all' enabled platforms",
        )
        search_parser.add_argument(
            "--headed", action="store_true", help="show the browser while searching"
        )

    subparsers.add_parser("platforms", help="list platforms, login state, and settings")
    login_parser = subparsers.add_parser(
        "login", help="log in to a platform yourself in a browser window (saved for reuse)"
    )
    login_parser.add_argument("platform")
    login_parser.add_argument("--timeout", type=_positive_int, default=10, help="minutes to wait")
    logout_parser = subparsers.add_parser("logout", help="delete a platform's saved login")
    logout_parser.add_argument("platform")
    logout_parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    subparsers.add_parser("status", help="pipeline counts, today's cap, platform sessions")
    pause_parser = subparsers.add_parser(
        "pause", help="stop the agent (searches stop, nothing starts)"
    )
    pause_parser.add_argument("reason", nargs="*", help="optional note")
    subparsers.add_parser("unpause", help="undo `jobpilot pause`")

    resume_parser = subparsers.add_parser("resume", help="resume management")
    resume_sub = resume_parser.add_subparsers(dest="resume_command", required=True)
    resume_import = resume_sub.add_parser("import", help="parse and store a resume PDF")
    resume_import.add_argument("path", type=Path)
    resume_import.add_argument("--version", default="default")
    resume_import.add_argument(
        "--no-activate", action="store_true", help="do not mark this version active"
    )
    resume_sub.add_parser("list", help="list stored resume versions")

    llm_parser = subparsers.add_parser("llm", help="local LLM management")
    llm_sub = llm_parser.add_subparsers(dest="llm_command", required=True)
    llm_sub.add_parser("check", help="verify Ollama server, model, and generation")

    match_parser = subparsers.add_parser("match", help="score jobs against the active resume")
    match_parser.add_argument("--job-id", type=int, help="score a single job")
    match_parser.add_argument("--limit", type=_positive_int, default=100, help="max jobs to score")

    rank_parser = subparsers.add_parser("rank", help="show ranked matches")
    rank_parser.add_argument("--top", type=_positive_int, default=20)
    rank_parser.add_argument("--min-score", type=_score, default=0, help="minimum LLM score 0-100")

    prepare_parser = subparsers.add_parser(
        "prepare", help="generate materials for a job and queue it for review"
    )
    prepare_parser.add_argument("job_id", type=int, help="job ID (see `jobpilot rank`)")

    subparsers.add_parser("review", help="review prepared applications in the terminal")

    apply_parser = subparsers.add_parser(
        "apply", help="open approved applications in a browser and autofill them"
    )
    apply_parser.add_argument(
        "application_ids", type=int, nargs="*", help="application IDs (default: all approved)"
    )

    run_parser = subparsers.add_parser("run", help="full pipeline: search → match → rank → prepare")
    run_parser.add_argument("--platform", "--source", dest="platform", default="all")
    run_parser.add_argument("--headed", action="store_true", help="show browsers while searching")
    run_parser.add_argument("--top", type=_positive_int, default=5, help="prepare top N matches")
    run_parser.add_argument(
        "--min-score",
        type=_score,
        default=0,
        help="minimum LLM score 0-100 (can only raise min_match_score from config.yaml)",
    )

    serve_parser = subparsers.add_parser("serve", help="run the dashboard API server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _score(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return number


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "db" and args.db_command == "init":
        return cmd_db_init()
    if args.command == "db" and args.db_command == "stats":
        return cmd_db_stats()
    if args.command == "config" and args.config_command == "show":
        return cmd_config_show()
    if args.command in ("search", "scrape"):
        return asyncio.run(_run_search(args.platform, args.headed))
    if args.command == "platforms":
        return asyncio.run(_run_platforms())
    if args.command == "login":
        return asyncio.run(_run_login(args.platform, args.timeout))
    if args.command == "logout":
        return asyncio.run(_run_logout(args.platform, args.yes))
    if args.command == "status":
        return asyncio.run(_run_status())
    if args.command == "pause":
        return cmd_pause(" ".join(args.reason))
    if args.command == "unpause":
        return cmd_unpause()
    if args.command == "resume" and args.resume_command == "import":
        return asyncio.run(_run_resume_import(args.path, args.version, not args.no_activate))
    if args.command == "resume" and args.resume_command == "list":
        return asyncio.run(_run_resume_list())
    if args.command == "llm" and args.llm_command == "check":
        return asyncio.run(_run_llm_check())
    if args.command == "match":
        return asyncio.run(_run_match(args.job_id, args.limit))
    if args.command == "rank":
        return asyncio.run(_run_rank(args.top, args.min_score))
    if args.command == "prepare":
        return asyncio.run(_run_prepare(args.job_id))
    if args.command == "review":
        return asyncio.run(_run_review())
    if args.command == "apply":
        return asyncio.run(_run_apply(args.application_ids))
    if args.command == "run":
        return asyncio.run(_run_pipeline(args.platform, args.top, args.min_score, args.headed))
    if args.command == "serve":
        import uvicorn

        from jobpilot.api.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
        return 0
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings: Settings = get_settings()
    except ValidationError as exc:
        console.print(f"[red]Invalid JOBPILOT_* environment settings:[/red]\n{exc}")
        return EXIT_CONFIG_ERROR
    setup_logging(settings.log_level, settings.logs_dir / "jobpilot.log")
    try:
        return _dispatch(args)
    except PreferencesError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return EXIT_CONFIG_ERROR
    except (LockBusyError, ProfileInUseError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return EXIT_BUSY
    except AgentPausedError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return EXIT_PAUSED
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow] Progress so far is saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
