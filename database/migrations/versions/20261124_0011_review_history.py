"""Review history, and the text a scope's screening rules read.

Revision: 0011_review_history
Revises: 0010_review_by_rule
Created: 2026-11-24

* ``review_steps``: a collection's review history, one row per step a person
  would name (scope approved, run frozen, scope rules applied, a filter applied,
  a pair merged), each with the scope version then in force and its counts.
* ``work_summaries.relevance_text``: title, abstract and subject terms,
  case-folded, so re-applying a scope's rules to every candidate reads one column
  instead of every assertion.

``review_steps`` is created from its table definition with ``checkfirst``, as
``0007`` does. The column is added only when missing: ``0010`` creates
``work_summaries`` from today's definition, which already has it.

Data migration notes: ``lab db backfill-review`` (run by
``scripts/deploy/update.sh`` after migrating) rebuilds every summary whose
``relevance_text`` is NULL, and, on a workspace with no history yet, records the
steps already taken (approvals, frozen runs, reconciliation passes, merges and
relevance decisions made by people) from the audit log and the decisions. It is
idempotent.

Rollback notes: ``downgrade`` drops the table and the column. Nothing outside
review reads either; the backfill rebuilds the column, and the decisions and the
audit chain keep everything the history summarised.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from lab_assistant.adapters.db.tables import review_steps

revision = "0011_review_history"
down_revision = "0010_review_by_rule"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    review_steps.create(bind=bind, checkfirst=True)
    if not _has_column("work_summaries", "relevance_text"):
        op.add_column("work_summaries", sa.Column("relevance_text", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    review_steps.drop(bind=bind, checkfirst=True)
    if _has_column("work_summaries", "relevance_text"):
        with op.batch_alter_table("work_summaries") as batch:
            batch.drop_column("relevance_text")
