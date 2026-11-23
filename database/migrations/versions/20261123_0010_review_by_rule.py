"""Per-work summaries and the duplicate-review queue.

Revision: 0010_review_by_rule
Revises: 0009_reconciliation_indexes
Created: 2026-11-23

Two tables for reviewing a collection by rule instead of one paper at a time:

* ``work_summaries``: per work, the canonical fields a filter reads (title, year,
  kind, journal, subject terms, sources). A filter over tens of thousands of
  works is then one indexed query, not a read of every assertion.
* ``duplicate_candidates``: one row per duplicate proposal with its review state
  (open, merged, dismissed, superseded), so a handled pair leaves the queue.

Created from the table definitions with ``checkfirst``, as ``0007`` does: a
database created from today's ``MetaData`` already has both.

Data migration notes: both tables start empty. ``lab db backfill-review``
(which ``scripts/deploy/update.sh`` runs after migrating) fills them from the
works, assertions and proposals already stored; it is idempotent.

Rollback notes: ``downgrade`` drops both tables. Nothing else reads them, and the
backfill rebuilds them.
"""

from __future__ import annotations

from alembic import op

from lab_assistant.adapters.db.tables import duplicate_candidates, work_summaries

revision = "0010_review_by_rule"
down_revision = "0009_reconciliation_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    work_summaries.create(bind=bind, checkfirst=True)
    duplicate_candidates.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    duplicate_candidates.drop(bind=bind, checkfirst=True)
    work_summaries.drop(bind=bind, checkfirst=True)
