"""An answer keeps its sources; a reader keeps each paper's authors.

``chat_messages.sources``: the sources an answer was written from, as the asker
was shown them. Until now only the citations were kept, so a conversation
reopened later showed no sources at all. Empty for every answer that exists
today: those show their cited papers, rebuilt from the citations, instead.

``work_metadata.authors``: the authors a snapshot carries for each paper, so a
mirror can name them beside a source. Empty until the next import brings them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_answer_sources"
down_revision = "0017_mirror_scope_membership"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _json_list(name: str) -> sa.Column:
    return sa.Column(
        name,
        JSONB().with_variant(sa.JSON(), "sqlite"),
        nullable=False,
        server_default=sa.text("'[]'"),
    )


def upgrade() -> None:
    if "sources" not in _columns("chat_messages"):
        op.add_column("chat_messages", _json_list("sources"))
    if "authors" not in _columns("work_metadata"):
        op.add_column("work_metadata", _json_list("authors"))


def downgrade() -> None:
    if "authors" in _columns("work_metadata"):
        op.drop_column("work_metadata", "authors")
    if "sources" in _columns("chat_messages"):
        op.drop_column("chat_messages", "sources")
