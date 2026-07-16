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


async def _run_resume_import(path: Path, version: str, activate: bool) -> int:
    from jobpilot.database import create_engine, create_session_factory
    from jobpilot.resume import import_resume

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        resume = await import_resume(
            create_session_factory(engine), path, version=version, activate=activate
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
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
