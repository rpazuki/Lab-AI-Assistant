"""The archive publication outbox.

Revision: 0002_publication_outbox
Revises: 0001_initial
Created: 2026-11-18

Adds ``archive_publications``: one row per attempt to move a library's head,
recording the publisher's intent before the swap and its knowledge after. It is
what lets a process that died mid-publication tell "I was about to publish" from
"I published and did not get to write it down" — see
``domain/publication.py`` for why those need different recoveries.

**This revision is conditional, on purpose.** ``0001_initial`` builds its schema
from the application's live ``MetaData`` with ``checkfirst=True``, so a database
created fresh *today* already gets this table from that revision, while one
migrated before today does not. Creating it unconditionally here would fail on
the first and succeed on the second. That asymmetry is the historical-schema
drift the audit records as F14, and the plan asks future migration work to
address it rather than hide it: until ``0001`` is pinned to a frozen schema,
every subsequent revision has to be written to be idempotent.

Data migration notes: none. The table starts empty; a library with no recorded
attempt and a head that already points somewhere is reconciled by reading the
head, which is the authority regardless of what this table says.

Rollback notes: ``downgrade`` drops the table, losing the record of in-flight
publications. A publication interrupted across a downgrade must be reconciled
from the head by hand.
"""

from __future__ import annotations

from alembic import op

from lab_assistant.adapters.db.tables import archive_publications

revision = "0002_publication_outbox"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `checkfirst` for the reason in the docstring, not as defensive habit.
    archive_publications.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    archive_publications.drop(bind=op.get_bind(), checkfirst=True)
