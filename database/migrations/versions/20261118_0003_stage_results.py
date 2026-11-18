"""The completed stage result index.

Revision: 0003_stage_results
Revises: 0002_publication_outbox
Created: 2026-11-18

Adds ``stage_results``: one row per distinct expensive input, recording how the
stage ended and the whole typed envelope that describes it.

It exists because reuse was decided by looking for output. Extraction asked
whether a claim revision carried the right fingerprint, so a paper the
extractor read and found *nothing* in — every target abstaining, which in this
system is a positive statement that the source does not report the value —
produced no revision, looked unprocessed, and was sent to the model again on
every restart. Forever, for every silent paper in the corpus. That is audit
finding F10, and it costs real money quietly.

Conditional for the same reason as ``0002``: ``0001_initial`` builds from the
application's live ``MetaData`` with ``checkfirst=True``, so a database created
today already has this table and one migrated earlier does not. Until ``0001``
is pinned to a frozen schema, every later revision has to be idempotent (F14).

Data migration notes: none, and deliberately no backfill. A row here asserts
that work was done and how it ended; inventing rows from existing claim
revisions would assert outcomes nobody recorded, and would silently mark every
silent paper as unprocessed anyway. The index fills as stages run.

Rollback notes: ``downgrade`` drops the table. The next run of each stage pays
for its input once more and the index refills.
"""

from __future__ import annotations

from alembic import op

from lab_assistant.adapters.db.tables import stage_results

revision = "0003_stage_results"
down_revision = "0002_publication_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    stage_results.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    stage_results.drop(bind=op.get_bind(), checkfirst=True)
