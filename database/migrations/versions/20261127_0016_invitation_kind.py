"""Invitations by temporary password, beside invitations by link.

Some people are easier to reach with a short password the admin sends them than
with a link: the admin's own mail client mangles long links, or the person signs
in from a machine where clicking one is awkward. Such an invitation is still an
invitation: its token is the temporary password, so it expires, is used once,
and is replaced by inviting the address again, by the rules a link already has.

One column, ``invitations.kind``: ``link`` for every row that exists today, so
nothing changes for an invitation already sent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_invitation_kind"
down_revision = "0015_workspace_roles"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "kind" not in _columns("invitations"):
        op.add_column(
            "invitations",
            sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'link'")),
        )


def downgrade() -> None:
    if "kind" in _columns("invitations"):
        op.drop_column("invitations", "kind")
