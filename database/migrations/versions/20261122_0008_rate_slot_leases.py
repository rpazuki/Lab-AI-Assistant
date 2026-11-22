"""Concurrency slots that expire.

Revision: 0008_rate_slot_leases
Revises: 0007_eval_reviews_gates
Created: 2026-11-22

Adds ``rate_budgets.slots_changed_at``: when a request last took or returned one
of the budget's concurrency slots. ``in_flight`` is a count, so a worker stopped
between taking a slot and returning it (a redeploy mid-request) leaked the slot
for good; two such stops filled a two-slot connector and every later request for
that source dead-lettered with "did not refill". A count no request has changed
for ``SLOT_LEASE_SECONDS`` is now reset to zero.

Conditional for the reason ``0002`` gives (F14): a database created from today's
``MetaData`` already has the column.

Data migration notes: none. Existing rows get a null change time, which the
budget reads as "predates the field": any slots such a row holds are reclaimed on
its next acquisition, which clears slots leaked before this release.

Rollback notes: ``downgrade`` drops the column; leaked slots again stay leaked
until the row is reset by hand.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_rate_slot_leases"
down_revision = "0007_eval_reviews_gates"
branch_labels = None
depends_on = None


def _present() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "slots_changed_at" in {
        column["name"] for column in inspector.get_columns("rate_budgets")
    }


def upgrade() -> None:
    if not _present():
        op.add_column("rate_budgets", sa.Column("slots_changed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    if _present():
        op.drop_column("rate_budgets", "slots_changed_at")
