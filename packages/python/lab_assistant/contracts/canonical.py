"""Canonical serialisation and fingerprinting.

Every reproducibility claim in this system reduces to two operations: turn a
value into exactly one byte string, and hash it. Both live here so that a
release manifest, an extraction cache key, and a task idempotency key cannot
disagree about what "the same input" means.

Rules:

* dict keys are sorted; ordering of a mapping never changes a digest;
* sequences keep their order, because order is meaningful in evidence
  selection and manifest listings;
* text is normalised to Unicode NFC before hashing, so a decomposed accent and
  a composed accent are the same fingerprint input;
* **payload serialisation accepts floats**; a rendition records bounding boxes,
  OCR confidences, and quality ratios, and refusing them would mean inventing a
  parallel encoding for real measurements;
* **fingerprint inputs refuse floats.** A digest is a claim that two runs had the
  same input, and an accumulated float (``0.1 + 0.2``) is not the number anyone
  configured. A caller with a genuinely authoritative float converts it through
  :func:`stable_number` at the call site that decided it was authoritative.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

__all__ = [
    "CanonicalisationError",
    "canonical_json",
    "canonical_value",
    "fingerprint",
    "sha256_bytes",
    "sha256_text",
    "short_digest",
    "stable_number",
]


class CanonicalisationError(TypeError):
    """A value cannot be canonicalised deterministically."""


def _normalise_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def stable_number(value: float | int | str | Decimal) -> str:
    """Convert a number to one stable textual form, deliberately.

    Floats are refused by :func:`canonical_value` because an accumulated float
    (``0.1 + 0.2``) is not the number anyone wrote down. A caller that genuinely
    holds a configured float — a sampling temperature, a threshold — converts it
    here, at a call site that has decided the value is authoritative.
    """
    if isinstance(value, str):
        return format(Decimal(value).normalize(), "f")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return format(Decimal(str(value)).normalize(), "f")


def canonical_value(value: Any, *, allow_floats: bool = True) -> Any:
    """Reduce ``value`` to JSON-safe primitives with deterministic ordering.

    ``allow_floats=False`` is used by :func:`fingerprint`; see the module
    docstring for why the two cases differ.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _normalise_text(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not allow_floats:
            raise CanonicalisationError(
                "floats are not fingerprintable; convert with stable_number() at the "
                "call site that owns the value"
            )
        return value
    if isinstance(value, Decimal):
        # `normalize()` collapses 1.10 and 1.1; exponent form is avoided by `format`.
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalisationError("naive datetime is not canonicalisable; attach UTC")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return sha256_bytes(value)
    if isinstance(value, Mapping):
        return {
            str(k): canonical_value(v, allow_floats=allow_floats)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted(canonical_value(v, allow_floats=allow_floats) for v in value)
    if isinstance(value, Sequence):
        return [canonical_value(v, allow_floats=allow_floats) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):  # pydantic BaseModel without importing pydantic here
        return canonical_value(model_dump(mode="python"), allow_floats=allow_floats)
    raise CanonicalisationError(f"cannot canonicalise {type(value)!r}")


def canonical_json(value: Any, *, allow_floats: bool = True) -> bytes:
    """Serialise ``value`` to canonical UTF-8 JSON bytes."""
    return json.dumps(
        canonical_value(value, allow_floats=allow_floats),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(_normalise_text(text).encode("utf-8"))


def fingerprint(*, kind: str, parts: Mapping[str, Any]) -> str:
    """Return a namespaced digest over ``parts``.

    ``kind`` is part of the digest input, so two different fingerprint kinds
    over identical parts never collide. This is the fix for the legacy
    extraction cache, where two different templates at version ``1`` could
    share a cache entry.
    """
    payload = canonical_json(
        {"kind": kind, "parts": canonical_value(parts, allow_floats=False)},
        allow_floats=False,
    )
    return f"{kind}:{sha256_bytes(payload)}"


def short_digest(digest: str, length: int = 12) -> str:
    """Human-readable prefix of a digest, for logs and UI labels only."""
    return digest.split(":")[-1][:length]
