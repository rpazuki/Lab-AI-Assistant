"""Initial schema: the evidence graph, tasks, releases, and projections.

Revision: 0001_initial
Revises: none
Created: 2026-08-12

The DDL is generated from the application's own ``MetaData``, which is what keeps
a hand-written migration from drifting from the ORM — the failure the predecessor
recorded in ``mistakes.md``. Extending this schema means adding a table to
``lab_assistant.adapters.db.tables`` and autogenerating the next revision.

Data migration notes: none. This is the first revision; legacy data arrives
through the importers in ``lab_assistant.migration``, which write through the
application services so every row carries its provenance.

Rollback notes: ``downgrade`` drops every table and therefore all data. It exists
for a scratch database, not for a populated one; a production rollback restores
from a backup instead.

pgvector: the extension is created before the per-dimension projection tables.
The vector *index* is created after the first projection build, because an
IVFFlat index needs representative data to build well and HNSW is better created
once the table exists — see ADR-0014.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from lab_assistant.adapters.db.tables import METADATA, embedding_table_for

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

#: Embedding dimensions this deployment can serve. Adding one is a new revision,
#: never an ALTER on an existing vector column.
DIMENSIONS = (64, 384, 768, 1024)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for dimension in DIMENSIONS:
        embedding_table_for(dimension)
    METADATA.create_all(bind=bind, checkfirst=True)

    if bind.dialect.name != "postgresql":
        return

    # Lexical retrieval index. Expression index on the same expression the query
    # uses, or PostgreSQL will not use it.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_chunks_fts "
        "ON search_chunks USING gin (to_tsvector('english', content))"
    )
    # Claim projections are queried by predicate and by subject.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_claim_projections_subject "
        "ON claim_projections (workspace_id, release_id, lower(subject))"
    )
    # The task claim query scans by state and stage; a partial index keeps it
    # cheap as completed rows accumulate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_claimable "
        "ON tasks (stage, priority, created_at) "
        "WHERE state in ('ready','leased')"
    )
    # An active alias must point at a published release. Enforced as a trigger
    # because it spans two tables.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab_alias_points_at_published() RETURNS trigger AS $$
        BEGIN
          IF NEW.release_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM corpus_releases
            WHERE id = NEW.release_id
              AND workspace_id = NEW.workspace_id
              AND status in ('published','superseded','publishing')
          ) THEN
            RAISE EXCEPTION 'alias % may only point at a published release', NEW.alias;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_alias_published ON search_aliases")
    op.execute(
        "CREATE TRIGGER trg_alias_published BEFORE INSERT OR UPDATE ON search_aliases "
        "FOR EACH ROW EXECUTE FUNCTION lab_alias_points_at_published()"
    )
    # Audit rows are append-only.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab_audit_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_events")
    op.execute(
        "CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION lab_audit_append_only()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS trg_alias_published ON search_aliases")
        op.execute("DROP FUNCTION IF EXISTS lab_audit_append_only()")
        op.execute("DROP FUNCTION IF EXISTS lab_alias_points_at_published()")
    for dimension in DIMENSIONS:
        embedding_table_for(dimension)
    METADATA.drop_all(bind=bind, checkfirst=True)


# Referenced so linters see the import as used; the tables are declared by import.
_ = sa
