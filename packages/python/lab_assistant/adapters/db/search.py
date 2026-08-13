"""PostgreSQL search projections.

Lexical retrieval uses ``tsvector`` on PostgreSQL and a LIKE-based fallback on
SQLite so the offline suite exercises the same code path. Vector retrieval uses
pgvector's distance operator when available, and an in-Python cosine otherwise.

Copying a projection is a digest-keyed insert-select: an unchanged release item
carries its rows forward rather than being re-embedded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from lab_assistant.adapters.db import tables as t
from lab_assistant.adapters.db.tables import embedding_table_for

__all__ = ["PostgresSearchIndex"]


class PostgresSearchIndex:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def _is_postgres(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    async def write_chunks(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int:
        if not rows:
            return 0
        payload = [
            {
                "chunk_key": row["chunk_key"],
                "workspace_id": workspace_id,
                "release_id": release_id,
                "work_id": UUID(str(row["work_id"])),
                "rendition_id": UUID(str(row["rendition_id"])),
                "node_ids": list(row["node_ids"]),
                "content": row["content"],
                "content_sha256": row["content_sha256"],
                "chunk_type": row["chunk_type"],
                "section_label": row.get("section_label"),
                "page": row.get("page"),
                "token_count": row["token_count"],
                "chunker_id": row["chunker_id"],
                "chunker_version": row["chunker_version"],
                "access_policy_snapshot_id": (
                    UUID(str(row["access_policy_snapshot_id"]))
                    if row.get("access_policy_snapshot_id")
                    else None
                ),
                "metadata_json": row.get("metadata", {}),
            }
            for row in rows
        ]
        async with self._engine.begin() as connection:
            for item in payload:
                # Chunk identity is content-derived, so a rerun is a no-op.
                exists = await connection.execute(
                    select(t.search_chunks.c.chunk_key).where(
                        t.search_chunks.c.chunk_key == item["chunk_key"],
                        t.search_chunks.c.release_id == release_id,
                    )
                )
                if exists.first() is None:
                    await connection.execute(insert(t.search_chunks).values(**item))
        return len(payload)

    async def copy_chunks(
        self,
        workspace_id: UUID,
        *,
        from_release_id: UUID,
        to_release_id: UUID,
        work_ids: Sequence[UUID],
    ) -> int:
        if not work_ids:
            return 0
        async with self._engine.begin() as connection:
            source = await connection.execute(
                select(t.search_chunks).where(
                    t.search_chunks.c.workspace_id == workspace_id,
                    t.search_chunks.c.release_id == from_release_id,
                    t.search_chunks.c.work_id.in_(list(work_ids)),
                )
            )
            copied = 0
            for row in source.mappings().all():
                values = dict(row)
                values["release_id"] = to_release_id
                exists = await connection.execute(
                    select(t.search_chunks.c.chunk_key).where(
                        t.search_chunks.c.chunk_key == values["chunk_key"],
                        t.search_chunks.c.release_id == to_release_id,
                    )
                )
                if exists.first() is None:
                    await connection.execute(insert(t.search_chunks).values(**values))
                copied += 1
        return copied

    async def write_embeddings(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        profile_id: str,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        table = embedding_table_for(len(rows[0]["vector"]))
        async with self._engine.begin() as connection:
            for row in rows:
                exists = await connection.execute(
                    select(table.c.chunk_key).where(
                        table.c.chunk_key == row["chunk_key"],
                        table.c.release_id == release_id,
                        table.c.profile_id == profile_id,
                    )
                )
                if exists.first() is not None:
                    continue
                await connection.execute(
                    insert(table).values(
                        chunk_key=row["chunk_key"],
                        workspace_id=workspace_id,
                        release_id=release_id,
                        work_id=UUID(str(row["work_id"])),
                        profile_id=profile_id,
                        vector=list(row["vector"]),
                    )
                )
        return len(rows)

    async def copy_embeddings(
        self,
        workspace_id: UUID,
        *,
        profile_id: str,
        from_release_id: UUID,
        to_release_id: UUID,
        work_ids: Sequence[UUID],
    ) -> int:
        if not work_ids:
            return 0
        copied = 0
        async with self._engine.begin() as connection:
            for dimension in _known_dimensions():
                table = embedding_table_for(dimension)
                source = await connection.execute(
                    select(table).where(
                        table.c.workspace_id == workspace_id,
                        table.c.release_id == from_release_id,
                        table.c.profile_id == profile_id,
                        table.c.work_id.in_(list(work_ids)),
                    )
                )
                for row in source.mappings().all():
                    values = dict(row)
                    values["release_id"] = to_release_id
                    exists = await connection.execute(
                        select(table.c.chunk_key).where(
                            table.c.chunk_key == values["chunk_key"],
                            table.c.release_id == to_release_id,
                            table.c.profile_id == profile_id,
                        )
                    )
                    if exists.first() is None:
                        await connection.execute(insert(table).values(**values))
                    copied += 1
        return copied

    async def write_claim_projection(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int:
        if not rows:
            return 0
        async with self._engine.begin() as connection:
            for row in rows:
                await connection.execute(
                    insert(t.claim_projections).values(
                        workspace_id=workspace_id,
                        release_id=release_id,
                        revision_id=UUID(str(row["revision_id"])),
                        claim_id=UUID(str(row["claim_id"])),
                        work_id=UUID(str(row["work_id"])),
                        experiment_id=(
                            UUID(str(row["experiment_id"])) if row.get("experiment_id") else None
                        ),
                        predicate=row["predicate"],
                        subject=row.get("subject"),
                        object_kind=row["object_kind"],
                        value_json=row["value"],
                        unit=row.get("unit"),
                        review_status=row["review_status"],
                        abstention_reason=row["abstention_reason"],
                        evidence_span_ids=[str(i) for i in row.get("evidence_span_ids", [])],
                        access_policy_snapshot_id=(
                            UUID(str(row["access_policy_snapshot_id"]))
                            if row.get("access_policy_snapshot_id")
                            else None
                        ),
                    )
                )
        return len(rows)

    async def lexical_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        query: str,
        top_k: int,
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [
            t.search_chunks.c.workspace_id == workspace_id,
            t.search_chunks.c.release_id == release_id,
        ]
        if work_ids is not None:
            if not work_ids:
                return []
            # Policy filtering happens in the candidate SQL, before ranking.
            conditions.append(t.search_chunks.c.work_id.in_(list(work_ids)))
        async with self._engine.connect() as connection:
            if self._is_postgres:
                rank = func.ts_rank(
                    func.to_tsvector(text("'english'"), t.search_chunks.c.content),
                    func.plainto_tsquery(text("'english'"), query),
                )
                statement = (
                    select(t.search_chunks, rank.label("score"))
                    .where(
                        *conditions,
                        func.to_tsvector(text("'english'"), t.search_chunks.c.content).op("@@")(
                            func.plainto_tsquery(text("'english'"), query)
                        ),
                    )
                    .order_by(rank.desc())
                    .limit(top_k)
                )
            else:
                terms = [token for token in query.lower().split() if len(token) > 1]
                if not terms:
                    return []
                like = None
                for term in terms:
                    clause = func.lower(t.search_chunks.c.content).like(f"%{term}%")
                    like = clause if like is None else (like | clause)
                statement = select(t.search_chunks).where(*conditions, like).limit(top_k)
            result = await connection.execute(statement)
            return [_chunk_row(row) for row in result.mappings().all()]

    async def vector_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        profile_id: str,
        vector: Sequence[float],
        top_k: int,
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]:
        table = embedding_table_for(len(vector))
        conditions = [
            table.c.workspace_id == workspace_id,
            table.c.release_id == release_id,
            table.c.profile_id == profile_id,
        ]
        if work_ids is not None:
            if not work_ids:
                return []
            conditions.append(table.c.work_id.in_(list(work_ids)))
        async with self._engine.connect() as connection:
            # A chunk key is content-derived, so the *same* key exists in every
            # release that carried that item forward. Joining on the key alone
            # therefore multiplies each hit by the number of releases sharing it —
            # inflating candidate counts and, worse, letting a stale release's row
            # be cited. The join is on the full projection key.
            join_on = (
                (t.search_chunks.c.chunk_key == table.c.chunk_key)
                & (t.search_chunks.c.release_id == table.c.release_id)
                & (t.search_chunks.c.workspace_id == table.c.workspace_id)
            )
            if self._is_postgres and hasattr(table.c.vector.type, "cosine_distance"):
                distance = table.c.vector.cosine_distance(list(vector))
                statement = (
                    select(t.search_chunks, distance.label("distance"))
                    .select_from(table.join(t.search_chunks, join_on))
                    .where(*conditions)
                    .order_by(distance)
                    .limit(top_k)
                )
                result = await connection.execute(statement)
                return [_chunk_row(row) for row in result.mappings().all()]

            # Fallback: score in Python. Correct, and only used off PostgreSQL.
            statement = select(table.c.chunk_key, table.c.vector).where(*conditions)
            rows = (await connection.execute(statement)).all()
            scored = sorted(
                ((_cosine(vector, row[1]), row[0]) for row in rows),
                key=lambda pair: (-pair[0], pair[1]),
            )[:top_k]
            keys = [key for _score, key in scored]
            if not keys:
                return []
            chunks = await connection.execute(
                select(t.search_chunks).where(
                    t.search_chunks.c.chunk_key.in_(keys),
                    t.search_chunks.c.release_id == release_id,
                    t.search_chunks.c.workspace_id == workspace_id,
                )
            )
            by_key = {row["chunk_key"]: _chunk_row(row) for row in chunks.mappings().all()}
            return [by_key[key] for key in keys if key in by_key]

    async def claim_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        predicate: str | None = None,
        subject: str | None = None,
        top_k: int = 20,
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [
            t.claim_projections.c.workspace_id == workspace_id,
            t.claim_projections.c.release_id == release_id,
        ]
        if work_ids is not None:
            if not work_ids:
                return []
            # Policy filtering happens in the candidate SQL, before ranking and
            # before the top-k window closes. Filtering afterwards would let
            # restricted claims decide which visible claims survive.
            conditions.append(t.claim_projections.c.work_id.in_(list(work_ids)))
        if predicate is not None:
            conditions.append(t.claim_projections.c.predicate == predicate)
        if subject is not None:
            conditions.append(func.lower(t.claim_projections.c.subject) == subject.lower())
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(t.claim_projections).where(*conditions).limit(top_k)
            )
            return [
                {
                    "revision_id": str(row["revision_id"]),
                    "claim_id": str(row["claim_id"]),
                    "work_id": str(row["work_id"]),
                    "experiment_id": (
                        str(row["experiment_id"]) if row["experiment_id"] else None
                    ),
                    "predicate": row["predicate"],
                    "subject": row["subject"],
                    "object_kind": row["object_kind"],
                    "value": row["value_json"],
                    "unit": row["unit"],
                    "review_status": row["review_status"],
                    "abstention_reason": row["abstention_reason"],
                    "evidence_span_ids": list(row["evidence_span_ids"] or []),
                }
                for row in result.mappings().all()
            ]

    async def count_chunks(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        work_ids: Sequence[UUID] | None = None,
    ) -> int:
        conditions = [
            t.search_chunks.c.workspace_id == workspace_id,
            t.search_chunks.c.release_id == release_id,
        ]
        if work_ids is not None:
            if not work_ids:
                return 0
            # An aggregate is a disclosure channel: a chunk total that includes
            # restricted works tells a caller how much material they cannot see.
            conditions.append(t.search_chunks.c.work_id.in_(list(work_ids)))
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(func.count()).select_from(t.search_chunks).where(*conditions)
            )
            return int(result.scalar() or 0)

    async def delete_release_projections(self, workspace_id: UUID, release_id: UUID) -> int:
        async with self._engine.begin() as connection:
            deleted = await connection.execute(
                delete(t.search_chunks).where(
                    t.search_chunks.c.workspace_id == workspace_id,
                    t.search_chunks.c.release_id == release_id,
                )
            )
            await connection.execute(
                delete(t.claim_projections).where(
                    t.claim_projections.c.workspace_id == workspace_id,
                    t.claim_projections.c.release_id == release_id,
                )
            )
            for dimension in _known_dimensions():
                table = embedding_table_for(dimension)
                await connection.execute(
                    delete(table).where(
                        table.c.workspace_id == workspace_id, table.c.release_id == release_id
                    )
                )
            return int(deleted.rowcount or 0)


def _chunk_row(row: Any) -> dict[str, Any]:
    return {
        "chunk_key": row["chunk_key"],
        "release_id": str(row["release_id"]),
        "work_id": str(row["work_id"]),
        "rendition_id": str(row["rendition_id"]),
        "node_ids": list(row["node_ids"] or []),
        "content": row["content"],
        "content_sha256": row["content_sha256"],
        "chunk_type": row["chunk_type"],
        "section_label": row["section_label"],
        "page": row["page"],
        "token_count": row["token_count"],
        "metadata": row["metadata_json"],
        "score": (float(row["score"]) if row.get("score") is not None else None),
    }


def _known_dimensions() -> tuple[int, ...]:
    from lab_assistant.adapters.db.tables import METADATA

    return tuple(
        int(name.rsplit("_", 1)[1])
        for name in METADATA.tables
        if name.startswith("chunk_embeddings_")
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    import math

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0
