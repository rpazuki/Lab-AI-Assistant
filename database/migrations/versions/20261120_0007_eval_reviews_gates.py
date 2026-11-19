"""Human review of evaluation answers, and benchmarks that gate publishing.

Revision: 0007_eval_reviews_gates
Revises: 0006_member_token_limits
Created: 2026-11-20

Adds two tables:

* ``evaluation_reviews`` — one row per (run, case, reviewer): an assignment that
  becomes a review when the reviewer submits grades. The grades are the
  predecessor's review form (five 1-5 scores, issue flags, confidence, comment,
  suggested answer).
* ``release_gate_benchmarks`` — per workspace, the benchmark versions a release
  must pass before it may be published.

Both are created with ``checkfirst``, for the reason ``0002`` gives (F14).

Data migration notes: none; both tables start empty, so no release is gated until
a steward requires a benchmark.

Rollback notes: ``downgrade`` drops both tables. Submitted human reviews are lost,
and publishing stops requiring any benchmark.
"""

from __future__ import annotations

from alembic import op

from lab_assistant.adapters.db.tables import evaluation_reviews, release_gate_benchmarks

revision = "0007_eval_reviews_gates"
down_revision = "0006_member_token_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    evaluation_reviews.create(bind=bind, checkfirst=True)
    release_gate_benchmarks.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    release_gate_benchmarks.drop(bind=bind, checkfirst=True)
    evaluation_reviews.drop(bind=bind, checkfirst=True)
