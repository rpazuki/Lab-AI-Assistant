"""Indexes reconciliation reads through.

Revision: 0009_reconciliation_indexes
Revises: 0008_rate_slot_leases
Created: 2026-11-22

Two lookups reconciliation makes once per record had no index, so each one read
its whole table, and both tables grow while the run is in progress:

* ``ix_source_assertions_observation``: a record's assertions, by observation.
  A 38,000-record run read the 600,000-row table once per record.
* ``ix_work_identifiers_work``: a work's identifiers, by work.
* ``ix_identity_decisions_reason``: the duplicate-review queue, which the
  collections page now reads a page at a time.

Conditional for the reason ``0002`` gives (F14): a database created from today's
``MetaData`` already has them.

Data migration notes: none. Building each index reads its table once; at the
sizes a lab corpus reaches that is seconds. Stop the compute worker first (the
deploy script does): an open reconciliation transaction would hold the build.

Rollback notes: ``downgrade`` drops the indexes; the queries still work, slowly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_reconciliation_indexes"
down_revision = "0008_rate_slot_leases"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_source_assertions_observation", "source_assertions", ["source_observation_id"]),
    ("ix_work_identifiers_work", "work_identifiers", ["workspace_id", "work_id"]),
    ("ix_identity_decisions_reason", "identity_decisions", ["workspace_id", "reason_code"]),
)


def _present(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        if not _present(table, name):
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in _INDEXES:
        if _present(table, name):
            op.drop_index(name, table_name=table)
