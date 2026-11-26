"""Roles an admin defines, for audiences the built-in ones do not name.

Patented material and confidential data do not divide along "researcher" and
"reviewer" lines. Who may reach them is a decision about *these people*, and it
outlives any one of them, so it needs a name of its own that a rule can be
written about once and keep meaning.

Three changes, all schema; no data moves.

``workspace_roles`` holds the definitions. A role carries no capabilities of its
own: ``base_role`` points at one of the built-in bundles, which is where its
members' capabilities come from, so defining a role can neither invent a
capability set nor mint an admin by accident.

``memberships.role_name`` and ``invitations.role_name`` hold the slug of the role
someone was put in, beside the built-in ``role`` they answer to. Null means the
built-in role is the whole story, which is every row that exists today — so
nothing changes for anyone until an admin defines a role and moves somebody into
it.

The slug is stored, not the id, because a rule names a role and a decision that
had to join to read the name would put a query in the path of every answer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_workspace_roles"
down_revision = "0014_scope_access"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "workspace_roles" not in _tables():
        op.create_table(
            "workspace_roles",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.Uuid(),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("slug", sa.Text(), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("base_role", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("created_by", sa.Uuid()),
            sa.UniqueConstraint("workspace_id", "slug", name="workspace_role_slug"),
        )
    for table in ("memberships", "invitations"):
        if "role_name" not in _columns(table):
            op.add_column(table, sa.Column("role_name", sa.Text()))


def downgrade() -> None:
    # The columns go first: a membership pointing at a role that no longer
    # exists would answer to a name nothing can grant.
    for table in ("memberships", "invitations"):
        if "role_name" in _columns(table):
            op.drop_column(table, "role_name")
    if "workspace_roles" in _tables():
        op.drop_table("workspace_roles")
