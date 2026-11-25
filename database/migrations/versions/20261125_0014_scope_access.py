"""Access control per scope: role grants, and every scope that exists today opened.

Two changes, one of them data.

**Schema.** A grant could only ever name a user, a group, a workspace or a service
account, each by id. A role has no id — it is a name on a membership — so
``subject_id`` becomes nullable and ``subject_role`` is added beside it, with
exactly one of the two set (the model enforces that; the database would need a
check constraint per dialect to say the same, which buys little here).

**Data.** Enforcement is new, and the corpus is not: this installation holds tens
of thousands of papers found by one scope, and without this every researcher and
reviewer would lose all of them the moment the new code ran. So every scope that
exists when this migration runs is opened to the three roles that could already
reach it — researcher, reviewer, data_steward. Admins need no grant.

A scope created *after* this migration starts closed, which is the point: access
is granted from here on, and nothing already relied upon is taken away. The
grants are ordinary rows, so opening or closing a scope later is an ordinary
change, and ``policy_basis`` says where these came from.
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0014_scope_access"
down_revision = "0013_site_pacing"
branch_labels = None
depends_on = None

_ROLE_INDEX = "ix_resource_grants_role"

#: The roles that could reach every scope before this ran. `admin` is left out
#: because the engine lets it through without a grant; `service_account` because
#: it never asks a question.
_OPENED_TO = ("researcher", "reviewer", "data_steward")

#: One action answers "may this person reach this scope's papers at all". What
#: they may then do with a paper — display it, export it — is still decided per
#: work, where it already was.
_ACTION = "query"

_BASIS = "migrated_existing_scope"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if "subject_role" not in _columns("resource_grants"):
        op.add_column("resource_grants", sa.Column("subject_role", sa.Text()))
    op.alter_column("resource_grants", "subject_id", existing_type=sa.Uuid(), nullable=True)
    if _ROLE_INDEX not in _indexes("resource_grants"):
        op.create_index(_ROLE_INDEX, "resource_grants", ["workspace_id", "subject_role", "action"])

    bind = op.get_bind()
    scopes = bind.execute(
        sa.text("select distinct workspace_id, scope_id from scope_versions")
    ).fetchall()
    for workspace_id, scope_id in scopes:
        for role in _OPENED_TO:
            # Re-running must not double the rows: a repeated grant is harmless to
            # the engine but noise to whoever reads the list.
            exists = bind.execute(
                sa.text(
                    "select 1 from resource_grants where workspace_id = :ws "
                    "and subject_role = :role and resource_id = :scope and action = :action"
                ),
                {"ws": workspace_id, "role": role, "scope": scope_id, "action": _ACTION},
            ).first()
            if exists is not None:
                continue
            bind.execute(
                sa.text(
                    "insert into resource_grants (id, workspace_id, subject_type, subject_id, "
                    "subject_role, resource_type, resource_id, action, effect, policy_basis) "
                    "values (:id, :ws, 'role', null, :role, 'scope', :scope, :action, "
                    "'allow', :basis)"
                ),
                {
                    "id": uuid4(),
                    "ws": workspace_id,
                    "role": role,
                    "scope": scope_id,
                    "action": _ACTION,
                    "basis": _BASIS,
                },
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("delete from resource_grants where policy_basis = :basis"), {"basis": _BASIS}
    )
    if _ROLE_INDEX in _indexes("resource_grants"):
        op.drop_index(_ROLE_INDEX, table_name="resource_grants")
    if "subject_role" in _columns("resource_grants"):
        op.drop_column("resource_grants", "subject_role")
    # `subject_id` is left nullable: rows written while it was may have none, and
    # a migration that fails on its own data is worse than a looser column.
