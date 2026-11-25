"""Pacing each publisher site, and searching candidates by language and abstract.

Revision: 0013_site_pacing
Revises: 0012_release_validated
Created: 2026-11-25

Adds ``acquisition_attempts.source_host``, the site that answered a fetch
(publisher, repository or API), so refusals can be counted per site; and
``rate_budgets.strikes``, how many times in a row a site pushed back (a rate
limit or a bot wall), which decides how long it rests before it is asked again.

Adds to ``work_summaries``: ``text_search`` (title, abstract and subject terms,
normalised for search), ``language`` (a two-letter code a source recorded or the
text showed) and ``language_detected``, with an index on the language.

Conditional for the reason ``0002`` gives (F14): ``0001_initial`` builds from the
live ``MetaData`` with ``checkfirst=True``, so a database created today already
has the columns. Columns are added only when missing, inspected rather than assumed.

Data migration notes: existing attempts have no site (they predate it), and
existing budgets start with no strikes. Existing summaries have no ``text_search``,
which is what ``lab db backfill-review`` (run by ``update.sh`` after every
migration) looks for: it rebuilds them, filling the language too.

Rollback notes: ``downgrade`` drops the columns and the index. Sites that were
resting keep their stored rest time but lose the count of refusals in a row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_site_pacing"
down_revision = "0012_release_validated"
branch_labels = None
depends_on = None

_ADDED = (
    ("acquisition_attempts", lambda: sa.Column("source_host", sa.Text())),
    (
        "rate_budgets",
        lambda: sa.Column("strikes", sa.Integer(), nullable=False, server_default=sa.text("0")),
    ),
    ("work_summaries", lambda: sa.Column("text_search", sa.Text())),
    ("work_summaries", lambda: sa.Column("language", sa.Text())),
    (
        "work_summaries",
        lambda: sa.Column(
            "language_detected", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    ),
)

_INDEX = "ix_work_summaries_language"


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column in _ADDED:
        added = column()
        if added.name not in _columns(table):
            op.add_column(table, added)
    if _INDEX not in _indexes("work_summaries"):
        op.create_index(_INDEX, "work_summaries", ["workspace_id", "language"])


def downgrade() -> None:
    if _INDEX in _indexes("work_summaries"):
        op.drop_index(_INDEX, table_name="work_summaries")
    for table, column in reversed(_ADDED):
        name = column().name
        if name in _columns(table):
            op.drop_column(table, name)
