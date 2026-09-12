"""job identity, pipeline status, platform sessions

Revision ID: 7c1e4a9b2d53
Revises: 28f0c73a14c4
Create Date: 2026-09-12 05:30:00

Jobs get a platform-aware identity (external_id, canonical_url, fingerprint)
and a pipeline status. dedup_hash is recomputed as the identity hash, so the
same posting reached via different tracking URLs is one row. Existing rows are
backfilled; if two old rows collapse to one identity, the later one is marked
a skipped duplicate instead of failing the migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1e4a9b2d53"
down_revision: str | None = "28f0c73a14c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill(connection: sa.Connection) -> None:
    # Imported here so a fresh database (no rows) never depends on app code.
    from jobpilot.domain.identity import canonicalize_url, identity_hash, job_fingerprint

    jobs = connection.execute(
        sa.text("SELECT id, source, company, title, application_url, scraped_at FROM jobs ORDER BY id")
    ).all()
    if not jobs:
        return
    app_status = dict(connection.execute(sa.text("SELECT job_id, status FROM applications")).all())
    matched = {row[0] for row in connection.execute(sa.text("SELECT DISTINCT job_id FROM match_results"))}
    to_job_status = {
        "submitted": "applied",
        "rejected": "rejected",
        "skipped": "skipped",
    }

    seen_hashes: dict[str, int] = {}
    for job_id, source, company, title, url, scraped_at in jobs:
        canonical = canonicalize_url(url)
        dedup = identity_hash(source, None, canonical)
        status, reason, duplicate_of = "discovered", "", None
        if job_id in app_status:
            status = to_job_status.get(app_status[job_id], "prepared")
        elif job_id in matched:
            status = "matched"
        if dedup in seen_hashes:
            # Two legacy rows are the same posting (URLs differed only by noise).
            duplicate_of = seen_hashes[dedup]
            dedup = identity_hash(source, f"legacy-duplicate-{job_id}", canonical)
            if status == "discovered":
                status, reason = "skipped", f"duplicate of #{duplicate_of}"
        else:
            seen_hashes[dedup] = job_id
        connection.execute(
            sa.text(
                "UPDATE jobs SET canonical_url=:canonical, dedup_hash=:dedup, "
                "fingerprint=:fingerprint, status=:status, status_reason=:reason, "
                "duplicate_of_id=:duplicate_of, last_seen_at=:seen WHERE id=:id"
            ),
            {
                "canonical": canonical,
                "dedup": dedup,
                "fingerprint": job_fingerprint(company, title),
                "status": status,
                "reason": reason,
                "duplicate_of": duplicate_of,
                "seen": scraped_at,
                "id": job_id,
            },
        )


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_id", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column("canonical_url", sa.String(length=2000), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("fingerprint", sa.String(length=32), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("status", sa.String(length=20), nullable=False, server_default="discovered")
        )
        batch_op.add_column(
            sa.Column("status_reason", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("duplicate_of_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_jobs_duplicate_of_id", "jobs", ["duplicate_of_id"], ["id"], ondelete="SET NULL"
        )
        # Old unique index on the raw URL: tracking parameters made it both too
        # strict (crashed re-scrapes) and too loose (same job, different URL).
        batch_op.drop_index("ix_jobs_application_url")

    _backfill(op.get_bind())

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_application_url", ["application_url"], unique=False)
        batch_op.create_index("ix_jobs_canonical_url", ["canonical_url"], unique=False)
        batch_op.create_index("ix_jobs_fingerprint", ["fingerprint"], unique=False)
        batch_op.create_index("ix_jobs_status", ["status"], unique=False)
        batch_op.create_index(
            "uq_jobs_source_external_id", ["source", "external_id"], unique=True
        )

    op.create_table(
        "platform_sessions",
        sa.Column("platform", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("platform"),
    )


def downgrade() -> None:
    op.drop_table("platform_sessions")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("uq_jobs_source_external_id")
        batch_op.drop_index("ix_jobs_status")
        batch_op.drop_index("ix_jobs_fingerprint")
        batch_op.drop_index("ix_jobs_canonical_url")
        batch_op.drop_index("ix_jobs_application_url")
        batch_op.drop_constraint("fk_jobs_duplicate_of_id", type_="foreignkey")
        for column in (
            "last_seen_at",
            "duplicate_of_id",
            "status_reason",
            "status",
            "fingerprint",
            "canonical_url",
            "external_id",
        ):
            batch_op.drop_column(column)
    # Restoring the old UNIQUE(application_url) could fail on data written since
    # the upgrade (the same URL under two identities), so it stays non-unique.
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_application_url", ["application_url"], unique=False)
