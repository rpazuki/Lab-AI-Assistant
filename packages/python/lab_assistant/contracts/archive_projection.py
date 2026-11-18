"""Chunks and vectors, in a form another installation can actually use.

Phase 2's fourth acceptance criterion is that **deleting the producer's SQL
projection does not lose published meaning**. That is only true if the
projection travels, and travelling is harder than it looks for two reasons.

**Floats do not round-trip through JSON.** Not reliably, not across parsers,
and not in a way anybody should bet a similarity score on. This repository
already refuses floats in fingerprint inputs for exactly that reason — an
accumulated float is not the number anyone configured. So a vector travels as
base64 of IEEE-754 **float32, little-endian**: a bit-exact round trip, about a
third larger than the raw bytes, and checkable, because the declared dimension
and the decoded length must agree or the row is rejected. The encoding is named
in every row rather than assumed, since the one thing worse than a wrong vector
is a wrong vector that decodes without complaint.

**float32 is the precision the vector store holds, not a shortcut.** pgvector's
`vector` is a 4-byte float, so this is lossless against what PostgreSQL
actually stores. A Python `float` is double precision, so a vector that has
only ever lived in memory is narrowed here — by exactly as much as writing it
to the database would narrow it. The archive carries what the corpus is served
from, which is the right answer, but it is a narrowing and not a bit-exact copy
of a float64 value, and saying otherwise would be a claim somebody later
measures.

**An index is not evidence.** HNSW parameters, IVFFlat lists, the physical
layout — none of it belongs in an archive. A reader builds its own index from
the vectors, tuned for its own hardware and query load. So the
`embedding_profile_digest` that decides whether vectors are *usable* covers the
model, its revision, the dimensions and the normalisation, and deliberately
**excludes** `index_kind` and `index_parameters`. Including them would have a
reader refuse vectors it could use perfectly well, and re-embed a corpus at
real cost, because the producer happened to prefer a different `ef_construction`.

What validation must catch before a reader trusts a vector: a dimension that
disagrees with the profile, a value that is not finite, and — where the profile
says the vectors are normalised — a norm that is not one. A NaN that reaches a
similarity search does not error; it quietly makes a document unreachable or
ranks it first, depending on the backend.
"""

from __future__ import annotations

import base64
import math
import struct
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "VECTOR_ENCODING",
    "ProjectionChunkRecord",
    "ProjectionVectorRecord",
    "chunker_digest",
    "decode_vector",
    "embedding_profile_digest",
    "encode_vector",
    "projection_record_id",
    "vector_problems",
]

PROJECTION_SCHEMA_VERSION = "1.0"

#: IEEE-754 single precision, little-endian, base64. Named in every row: a
#: reader must never have to infer how to read a number.
VECTOR_ENCODING = "float32-le-base64"

#: Namespace for deriving a stable row id from (release, chunk). Derived so two
#: builds of one projection agree, which is what keeps a shard digest stable.
_PROJECTION_NAMESPACE = UUID("1b4e28ba-2fa1-11d2-883f-0016d3cca427")

#: How far a normalised vector may be from unit length before it is a finding.
#: Generous enough for float32 accumulation over a few thousand dimensions,
#: tight enough that an unnormalised vector cannot hide.
_NORM_TOLERANCE = 1e-3


def encode_vector(values: list[float] | tuple[float, ...]) -> str:
    """Base64 of float32 little-endian. Bit-exact on the way back."""
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def decode_vector(encoded: str) -> list[float]:
    """The values back, or a `ValueError` naming what was wrong.

    Raises rather than returning a partial vector: half a vector scores as
    confidently as a whole one and is wrong in a way nothing downstream checks.
    """
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(f"vector is not valid base64 ({exc})") from exc
    if len(raw) % 4:
        raise ValueError(
            f"vector is {len(raw)} bytes, which is not a whole number of float32 values"
        )
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def vector_problems(
    values: list[float], *, dimensions: int, normalisation: str | None
) -> list[str]:
    """Everything wrong with a decoded vector, in a reader's terms.

    Returns all of them rather than the first: an operator looking at a bad
    shard wants the shape of the problem, not a guided tour one row at a time.
    """
    problems: list[str] = []
    if len(values) != dimensions:
        problems.append(f"vector has {len(values)} values and the profile declares {dimensions}")
    non_finite = sum(1 for value in values if not math.isfinite(value))
    if non_finite:
        # A NaN in a similarity search does not raise. It makes a document
        # unreachable or ranks it first, depending on the backend, and nothing
        # downstream notices.
        problems.append(f"{non_finite} value(s) are not finite (NaN or infinity)")
    if normalisation == "l2" and values and not non_finite:
        norm = math.sqrt(sum(value * value for value in values))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            problems.append(
                f"the profile says vectors are l2-normalised and this one has norm {norm:.6f}"
            )
    return problems


def projection_record_id(release_id: UUID, chunk_key: str, suffix: str = "") -> UUID:
    return uuid5(_PROJECTION_NAMESPACE, f"{release_id}:{chunk_key}:{suffix}")


def chunker_digest(chunker_id: str, chunker_version: str) -> str:
    """What produced these chunk boundaries.

    A reader with the same digest may reuse spans and offsets against its own
    renditions; a different one must not, because the boundaries moved.
    """
    return sha256_bytes(
        canonical_json({"chunker_id": chunker_id, "chunker_version": chunker_version})
    )


def embedding_profile_digest(profile: Any) -> str:
    """What makes these vectors comparable — and nothing about the index.

    Model, revision, dimensions and normalisation decide whether a query
    embedded by a reader can be compared with these vectors at all. `index_kind`
    and `index_parameters` do not: the reader builds its own index, tuned for
    its own hardware. Folding them in would make a reader re-embed a corpus
    because the producer preferred a different `ef_construction`, which is a
    real cost for no safety.
    """
    return sha256_bytes(
        canonical_json(
            {
                "profile_id": getattr(profile, "profile_id", None),
                "provider": getattr(profile, "provider", None),
                "model": getattr(profile, "model", None),
                "model_revision": getattr(profile, "model_revision", None),
                "dimensions": getattr(profile, "dimensions", None),
                "normalisation": getattr(profile, "normalisation", None),
            }
        )
    )


class ProjectionChunkRecord(BaseModel):
    """One retrievable passage, as the release serves it.

    The content travels. A reader could in principle re-chunk the rendition and
    get the same passages, but only if its chunker is bit-identical — and if it
    is not, every stored offset and every citation into this release moves.
    Carrying the text makes the projection self-contained and the citations
    stable, which is the point of publishing a release at all.
    """

    model_config = {"frozen": True}

    kind: str = "projection_chunk"
    schema_version: str = PROJECTION_SCHEMA_VERSION
    library_id: UUID
    record_id: UUID
    release_id: UUID
    #: Content-derived, and shared across releases by design: an unchanged
    #: item's chunk carries the same key into the successor release, which is
    #: what makes an incremental build a copy rather than a recompute.
    chunk_key: str
    work_id: UUID
    rendition_id: UUID
    node_ids: tuple[str, ...] = ()
    content: str
    content_sha256: str
    chunk_type: str
    section_label: str | None = None
    page: int | None = None
    token_count: int = 0
    chunker_id: str
    chunker_version: str
    #: The policy snapshot in force when this chunk was projected. An importer
    #: applies it: a chunk is servable material, and serving it without its
    #: access decision is how restricted content leaks.
    access_policy_snapshot_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectionVectorRecord(BaseModel):
    """One embedding, encoded so it arrives as the number that was computed."""

    model_config = {"frozen": True}

    kind: str = "projection_vector"
    schema_version: str = PROJECTION_SCHEMA_VERSION
    library_id: UUID
    record_id: UUID
    release_id: UUID
    chunk_key: str
    profile_id: str
    dimensions: int
    #: Named, never assumed. A reader that guessed the layout would decode a
    #: plausible-looking vector of the wrong numbers.
    encoding: str = VECTOR_ENCODING
    vector: str

    def values(self) -> list[float]:
        return decode_vector(self.vector)
