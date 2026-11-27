"""Which scopes found each paper, on a reader.

A publisher works out which scopes a paper belongs to from the discovery runs
that found it. A reader has no discovery runs, so the snapshot now carries the
answer on each work, and the reader keeps it beside the title it already keeps:
``work_metadata.scope_ids``. A mirror's admin can then stop serving a scope, and
the papers only that scope found go with it.

Empty for every row that exists today: those papers sit under no scope until the
next import brings their membership, which is what they were before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017_mirror_scope_membership"
down_revision = "0016_invitation_kind"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "scope_ids" not in _columns("work_metadata"):
        op.add_column(
            "work_metadata",
            sa.Column(
                "scope_ids",
                JSONB().with_variant(sa.JSON(), "sqlite"),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    if "scope_ids" in _columns("work_metadata"):
        op.drop_column("work_metadata", "scope_ids")
