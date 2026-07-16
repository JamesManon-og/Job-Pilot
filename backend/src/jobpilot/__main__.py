"""JobPilot CLI: `python -m jobpilot <command>`.

Commands:
    db init    Create/upgrade the database schema (runs Alembic migrations).
    db stats   Print row counts for every table.
    config show  Print the resolved settings and user preferences.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from jobpilot.llm import OllamaClient
    from jobpilot.matcher import MatchService

from alembic import command
from alembic.config import Config as AlembicConfig
from rich.console import Console
from rich.table import Table

from jobpilot.config import get_settings, load_preferences
from jobpilot.logging_setup import setup_logging

logger = logging.getLogger(__name__)
console = Console()


def _alembic_config() -> AlembicConfig:
    backend_dir = Path(__file__).resolve().parents[2]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    return cfg


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
    async with session_factory() as session:
        for table in Base.metadata.sorted_tables:
            count = await session.scalar(select(func.count()).select_from(table))
            stats[table.name] = count or 0
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


async def _run_scrape(sources: list[str]) -> int:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.domain.enums import JobSource
    from jobpilot.domain.models import ScrapeRun
    from jobpilot.scrapers import ScrapeRunner, all_scrapers

    settings = get_settings()
    preferences = load_preferences(settings.preferences_path)
    engine = create_engine(settings.database_url)
    runner = ScrapeRunner(create_session_factory(engine), preferences)

    targets = list(all_scrapers()) if sources == ["all"] else [JobSource(name) for name in sources]
    results: list[ScrapeRun] = []
    for source in targets:
        results.append(await runner.run(source))
    await engine.dispose()

    table = Table(title="Scrape results")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Found", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Error")
    for run in results:
        table.add_row(
            run.source.value,
            run.status.value,
            str(run.jobs_found),
            str(run.jobs_new),
            run.error or "",
        )
    console.print(table)
    return 0 if all(run.error is None for run in results) else 1


def cmd_scrape(source: str) -> int:
    return asyncio.run(_run_scrape([source]))


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
    async with factory() as session:
        resumes = await ResumeRepository(session).list()
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


async def _match_service() -> tuple[MatchService, AsyncEngine]:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.matcher import MatchEngine, MatchService

    settings = get_settings()
    llm = await _configured_llm()
    if llm is None:
        console.print(
            "[red]LLM not ready.[/red] Run [bold]jobpilot llm check[/bold] for diagnostics."
        )
        raise SystemExit(1)
    engine = create_engine(settings.database_url)
    service = MatchService(
        create_session_factory(engine),
        MatchEngine(llm, model_name=llm.model),
        load_preferences(settings.preferences_path),
    )
    return service, engine


async def _run_match(job_id: int | None, limit: int) -> int:
    from jobpilot.matcher import NoActiveResumeError

    service, engine = await _match_service()
    try:
        if job_id is not None:
            result = await service.match_one(job_id)
            results = [result]
        else:
            results = await service.match_unscored(limit=limit)
    except (NoActiveResumeError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    finally:
        await engine.dispose()

    console.print(f"[green]Scored {len(results)} job(s).[/green]")
    return 0


async def _run_rank(top: int, min_score: int) -> int:
    from jobpilot.matcher import NoActiveResumeError

    service, engine = await _match_service()
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


async def _run_apply(job_id: int) -> int:
    from jobpilot.applications import ApplicationService, DailyCapReachedError
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.database.repositories import DuplicateApplicationError, ResumeRepository

    settings = get_settings()
    preferences = load_preferences(settings.preferences_path)
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)

    async with factory() as session:
        resume = await ResumeRepository(session).get_active()
    if resume is None or resume.id is None:
        console.print("[red]No active resume.[/red] Run: jobpilot resume import <path>")
        await engine.dispose()
        return 1

    llm = await _configured_llm()
    service = ApplicationService(factory, preferences, llm=llm)

    try:
        application = await service.prepare(job_id, resume_id=resume.id)
    except DuplicateApplicationError:
        console.print("[yellow]Already applied to this job.[/yellow]")
        await engine.dispose()
        return 1
    except DailyCapReachedError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        await engine.dispose()
        return 1
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        await engine.dispose()
        return 1

    await engine.dispose()
    console.print(
        f"[green]Application #{application.id} prepared[/green] "
        f"(status: {application.status.value})"
    )
    if application.cover_letter:
        console.print(f"[dim]Cover letter: {len(application.cover_letter)} chars[/dim]")
    if preferences.human_approval_enabled:
        console.print("Review it in the dashboard, then approve to submit.")
    return 0


def cmd_config_show() -> int:
    settings = get_settings()
    preferences = load_preferences(settings.preferences_path)
    console.print("[bold]Settings[/bold]")
    console.print_json(settings.model_dump_json())
    console.print("[bold]Preferences[/bold]")
    console.print_json(preferences.model_dump_json())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("db", help="database management")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="create/upgrade the database schema")
    db_sub.add_parser("stats", help="print row counts per table")

    config_parser = subparsers.add_parser("config", help="configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="print resolved settings and preferences")

    scrape_parser = subparsers.add_parser("scrape", help="scrape job sources")
    scrape_parser.add_argument(
        "--source", default="all", help="source name (e.g. remoteok) or 'all'"
    )

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
    match_parser.add_argument("--limit", type=int, default=100, help="max jobs to score")

    rank_parser = subparsers.add_parser("rank", help="show ranked matches")
    rank_parser.add_argument("--top", type=int, default=20)
    rank_parser.add_argument("--min-score", type=int, default=0)

    apply_parser = subparsers.add_parser("apply", help="prepare an application for a job")
    apply_parser.add_argument("job_id", type=int, help="job ID to apply to")

    serve_parser = subparsers.add_parser("serve", help="run the dashboard API server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.logs_dir / "jobpilot.log")

    if args.command == "db" and args.db_command == "init":
        return cmd_db_init()
    if args.command == "db" and args.db_command == "stats":
        return cmd_db_stats()
    if args.command == "config" and args.config_command == "show":
        return cmd_config_show()
    if args.command == "scrape":
        return cmd_scrape(args.source)
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
    if args.command == "apply":
        return asyncio.run(_run_apply(args.job_id))
    if args.command == "serve":
        import uvicorn

        from jobpilot.api.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
