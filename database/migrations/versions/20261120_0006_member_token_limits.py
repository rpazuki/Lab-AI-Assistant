"""Per-member token limits.

Revision: 0006_member_token_limits
Revises: 0005_evaluation_runs
Created: 2026-11-20

Adds ``memberships.token_limit``: the tokens a member may use for answers in
total, set by an admin. Null means the installation default
(``LAB_CHAT_TOKEN_LIMIT_DEFAULT``), which is what every existing membership gets.

Conditional for the reason ``0002`` gives (F14): a database created from today's
``MetaData`` already has the column.

Data migration notes: none. Usage is computed from ``chat_messages``, which has
recorded ``input_tokens`` and ``output_tokens`` since ``0001``; answers streamed
before this release recorded a chunk count instead of tokens, so a member's usage
before the upgrade is an underestimate.

Rollback notes: ``downgrade`` drops the column, and every member returns to the
default limit.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_member_token_limits"
down_revision = "0005_evaluation_runs"
branch_labels = None
depends_on = None


def _present() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "token_limit" in {column["name"] for column in inspector.get_columns("memberships")}


def upgrade() -> None:
    if not _present():
        op.add_column("memberships", sa.Column("token_limit", sa.Integer()))


def downgrade() -> None:
    if _present():
        op.drop_column("memberships", "token_limit")
