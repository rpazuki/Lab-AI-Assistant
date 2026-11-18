"""A reader's import record and its imported work metadata.

Revision: 0004_archive_imports
Revises: 0003_stage_results
Created: 2026-11-18

Adds ``archive_imports`` — one row per snapshot import, the newest active one
being the reader's durable position, which is what refuses a replayed head —
and ``work_metadata``, the titles and years a reader cannot reconcile from
source assertions because it has none.

Conditional for the reason ``0002`` gives (F14): ``0001_initial`` builds from
live ``MetaData`` with ``checkfirst=True``.

Data migration notes: none. Rollback notes: ``downgrade`` drops both tables; a
reader then re-imports, and refuses nothing it would have refused, until its
first import records a position again.
"""

from __future__ import annotations

from alembic import op

from lab_assistant.adapters.db.tables import archive_imports, work_metadata

revision = "0004_archive_imports"
down_revision = "0003_stage_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    archive_imports.create(bind=op.get_bind(), checkfirst=True)
    work_metadata.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    work_metadata.drop(bind=op.get_bind(), checkfirst=True)
    archive_imports.drop(bind=op.get_bind(), checkfirst=True)
