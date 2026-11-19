"""Registered benchmarks, and evaluation runs that exist before they finish.

Revision: 0005_evaluation_runs
Revises: 0004_archive_imports
Created: 2026-11-19

Adds ``benchmarks`` — one immutable row per benchmark version an operator
registers — and gives ``evaluation_runs`` a lifecycle: ``status``, ``kind``,
``task_id``, ``requested_by``, ``error_detail`` and ``completed_at``. A run is now
recorded when it is requested and completed by the evaluation worker, so a queued
or failed run is visible rather than absent.

Conditional for the reason ``0002`` gives (F14): ``0001_initial`` builds from the
live ``MetaData`` with ``checkfirst=True``, so a database created today already
has the table and the columns, and one migrated earlier has neither. Columns are
added only when missing, inspected rather than assumed.

Data migration notes: existing ``evaluation_runs`` rows take ``status`` =
``succeeded`` from the server default. They were only ever written complete.

Rollback notes: ``downgrade`` drops ``benchmarks`` and the added columns. Runs
requested but not finished lose their status and read as complete; re-run them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from lab_assistant.adapters.db.tables import benchmarks, evaluation_runs

revision = "0005_evaluation_runs"
down_revision = "0004_archive_imports"
branch_labels = None
depends_on = None


def _added() -> tuple[sa.Column, ...]:
    """The columns 0005 adds, matching the live table's definitions."""
    return (
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'succeeded'")),
        sa.Column("kind", sa.Text()),
        sa.Column("task_id", evaluation_runs.c.task_id.type),
        sa.Column(
            "requested_by",
            evaluation_runs.c.requested_by.type,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("error_detail", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("evaluation_runs")}


def upgrade() -> None:
    benchmarks.create(bind=op.get_bind(), checkfirst=True)
    present = _existing_columns()
    for column in _added():
        if column.name not in present:
            op.add_column("evaluation_runs", column)


def downgrade() -> None:
    present = _existing_columns()
    for column in reversed(_added()):
        if column.name in present:
            op.drop_column("evaluation_runs", column.name)
    benchmarks.drop(bind=op.get_bind(), checkfirst=True)
