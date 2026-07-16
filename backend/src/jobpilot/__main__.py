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

    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.logs_dir / "jobpilot.log")

    if args.command == "db" and args.db_command == "init":
        return cmd_db_init()
    if args.command == "db" and args.db_command == "stats":
        return cmd_db_stats()
    if args.command == "config" and args.config_command == "show":
        return cmd_config_show()
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
