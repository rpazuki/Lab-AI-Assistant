"""In-memory search index.

Mirrors the PostgreSQL adapter's contract exactly: rows are release-scoped, a
copy is by release item, and lexical scoring is a bag-of-words overlap rather than
anything clever. It exists so the whole retrieval and release path is testable
without a database, and so a projection-membership bug shows up in the fast suite.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

__all__ = ["MemorySearchIndex"]


def _tokenise(text: str) -> list[str]:
    return [
        token
        for token in "".join(char.lower() if char.isalnum() else " " for char in text).split()
        if len(token) > 1
    ]


@dataclass
class MemorySearchIndex:
    chunks: dict[tuple[UUID, UUID], dict[str, dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    embeddings: dict[tuple[UUID, UUID, str], dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    claims: dict[tuple[UUID, UUID], list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    async def write_chunks(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int:
        target = self.chunks[(workspace_id, release_id)]
        for row in rows:
            target[row["chunk_key"]] = dict(row)
        return len(rows)

    async def copy_chunks(
        self,
        workspace_id: UUID,
        *,
        from_release_id: UUID,
        to_release_id: UUID,
        work_ids: Sequence[UUID],
    ) -> int:
        source = self.chunks[(workspace_id, from_release_id)]
        target = self.chunks[(workspace_id, to_release_id)]
        wanted = {str(work_id) for work_id in work_ids}
        copied = 0
        for key, row in source.items():
            if row.get("work_id") not in wanted:
                continue
            clone = dict(row)
            clone["release_id"] = str(to_release_id)
            target[key] = clone
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
        target = self.embeddings[(workspace_id, release_id, profile_id)]
        for row in rows:
            target[row["chunk_key"]] = list(row["vector"])
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
        source = self.embeddings[(workspace_id, from_release_id, profile_id)]
        target = self.embeddings[(workspace_id, to_release_id, profile_id)]
        chunk_rows = self.chunks[(workspace_id, from_release_id)]
        wanted = {str(work_id) for work_id in work_ids}
        copied = 0
        for key, vector in source.items():
            row = chunk_rows.get(key)
            if row is None or row.get("work_id") not in wanted:
                continue
            target[key] = list(vector)
            copied += 1
        return copied

    async def write_claim_projection(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int:
        self.claims[(workspace_id, release_id)].extend(dict(row) for row in rows)
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
        allowed = {str(work_id) for work_id in work_ids} if work_ids is not None else None
        query_tokens = set(_tokenise(query))
        if not query_tokens:
            return []
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, row in self.chunks[(workspace_id, release_id)].items():
            if allowed is not None and row.get("work_id") not in allowed:
                continue
            tokens = _tokenise(row["content"])
            if not tokens:
                continue
            overlap = sum(1 for token in tokens if token in query_tokens)
            if not overlap:
                continue
            # Length-normalised overlap: a long chunk does not win by volume.
            score = overlap / math.sqrt(len(tokens))
            scored.append((score, key, row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [dict(row, score=round(score, 6)) for score, _key, row in scored[:top_k]]

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
        allowed = {str(work_id) for work_id in work_ids} if work_ids is not None else None
        rows = self.chunks[(workspace_id, release_id)]
        vectors = self.embeddings[(workspace_id, release_id, profile_id)]
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, stored in vectors.items():
            row = rows.get(key)
            if row is None:
                continue
            if allowed is not None and row.get("work_id") not in allowed:
                continue
            scored.append((_cosine(vector, stored), key, row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [dict(row, score=round(score, 6)) for score, _key, row in scored[:top_k]]

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
        allowed = {str(work_id) for work_id in work_ids} if work_ids is not None else None
        if allowed is not None and not allowed:
            return []
        rows = self.claims[(workspace_id, release_id)]
        selected = [
            row
            for row in rows
            # Before the top-k window closes, not after: a restricted claim must not
            # decide which visible claim survives truncation.
            if (allowed is None or str(row.get("work_id")) in allowed)
            and (predicate is None or row.get("predicate") == predicate)
            and (subject is None or str(row.get("subject") or "").casefold() == subject.casefold())
        ]
        return [dict(row) for row in selected[:top_k]]

    async def count_chunks(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        work_ids: Sequence[UUID] | None = None,
    ) -> int:
        rows = self.chunks[(workspace_id, release_id)]
        if work_ids is None:
            return len(rows)
        allowed = {str(work_id) for work_id in work_ids}
        if not allowed:
            return 0
        return sum(1 for row in rows.values() if str(row.get("work_id")) in allowed)

    async def delete_release_projections(self, workspace_id: UUID, release_id: UUID) -> int:
        removed = len(self.chunks.pop((workspace_id, release_id), {}))
        self.claims.pop((workspace_id, release_id), None)
        for key in [k for k in self.embeddings if k[0] == workspace_id and k[1] == release_id]:
            self.embeddings.pop(key, None)
        return removed


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError(
            f"vector dimension mismatch: {len(left)} against {len(right)}; "
            "vectors of different dimensions never share a projection"
        )
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)
