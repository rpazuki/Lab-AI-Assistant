"""Passing validation makes a release validated, not approved.

Revision: 0012_release_validated
Revises: 0011_review_history
Created: 2026-11-24

Validation used to set a release to ``approved`` whenever every gate passed, so
a release was approved without anyone approving it. Now it becomes ``validated``
and a person approves it, which records their name and queues indexing.

``corpus_releases.status`` is free text (no CHECK lists the states), so there is
no schema change. Data migration: a release left ``approved`` by validation alone
(``approved_by`` is NULL: nobody approved it) goes back to ``validated``, where
it waits for a person. A release a person approved keeps its state.

Rollback notes: ``downgrade`` turns ``validated`` back into ``approved``, which is
what the previous code would have made it.
"""

from __future__ import annotations

from alembic import op

revision = "0012_release_validated"
down_revision = "0011_review_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE corpus_releases SET status = 'validated' "
        "WHERE status = 'approved' AND approved_by IS NULL"
    )


def downgrade() -> None:
    op.execute("UPDATE corpus_releases SET status = 'approved' WHERE status = 'validated'")
