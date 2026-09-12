"""Schema migrations against legacy data (not just an empty database)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

import jobpilot.__main__ as cli


def _config(db: Path):  # type: ignore[no-untyped-def]
    cfg = cli._alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    return cfg


def _insert_job(conn: sqlite3.Connection, url: str, title: str = "Engineer") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (title, company, employment_type, experience_level, remote, "
        "description, requirements, benefits, technologies, application_url, source, "
        "scraped_at, dedup_hash) VALUES (?, 'Acme', 'unknown', 'unknown', 'remote', '', "
        "'[]', '[]', '[]', ?, 'remoteok', '2026-07-16 04:22:31', ?)",
        (title, url, f"legacy-{url}"),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_identity_migration_backfills_legacy_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    cfg = _config(db)
    command.upgrade(cfg, "28f0c73a14c4")

    with sqlite3.connect(db) as conn:
        first = _insert_job(conn, "https://remoteok.com/remote-jobs/1")
        # Same posting reached with a tracking parameter: legal under the old
        # UNIQUE(application_url), but one identity after canonicalization.
        dup = _insert_job(conn, "https://remoteok.com/remote-jobs/1?utm_source=feed")
        applied = _insert_job(conn, "https://remoteok.com/remote-jobs/2", "Designer")
        conn.execute(
            "INSERT INTO resumes (version, file_path, profile, is_active, created_at) "
            "VALUES ('v1', '/r.pdf', '{}', 1, '2026-07-16')"
        )
        conn.execute(
            "INSERT INTO applications (job_id, resume_id, status, cover_letter, answers, notes, "
            "created_at) VALUES (?, 1, 'submitted', '', '{}', '', '2026-07-16')",
            (applied,),
        )

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT id, status, duplicate_of_id, canonical_url, dedup_hash FROM jobs"
            )
        }
        assert rows[first][0] == "discovered"
        assert rows[dup][:2] == ("skipped", first)
        assert rows[first][2] == rows[dup][2] == "https://remoteok.com/remote-jobs/1"
        assert rows[applied][0] == "applied"
        assert len({r[3] for r in rows.values()}) == 3  # dedup hashes stay unique
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(cfg, "28f0c73a14c4")
    command.upgrade(cfg, "head")
