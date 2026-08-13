"""Append-only audit trail with a per-workspace hash chain."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.contracts.events import ActorType, AuditAction, AuditEventRecord
from lab_assistant.contracts.runtime import Clock, IdFactory

__all__ = ["AuditTrail", "latest_hash", "verify_chain"]


class AuditTrail:
    """Builds chained audit events.

    The caller supplies the previous hash for the workspace; the repository keeps
    it. Chaining means a deleted or reordered row is detectable without trusting
    row ids or timestamps.
    """

    def __init__(self, *, id_factory: IdFactory, clock: Clock) -> None:
        self._new_id = id_factory
        self._clock = clock

    def build(
        self,
        *,
        workspace_id: UUID | None,
        actor_type: ActorType,
        actor_id: UUID | None,
        action: AuditAction,
        subject_type: str,
        subject_id: UUID | None,
        previous_hash: str | None,
        run_id: UUID | None = None,
        request_id: str | None = None,
        reason_code: str | None = None,
        before_object_id: UUID | None = None,
        after_object_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEventRecord:
        record = AuditEventRecord(
            id=self._new_id(),
            workspace_id=workspace_id,
            occurred_at=occurred_at or self._clock(),
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            run_id=run_id,
            request_id=request_id,
            reason_code=reason_code,
            before_object_id=before_object_id,
            after_object_id=after_object_id,
            metadata=_redact(metadata or {}),
        )
        return record.with_integrity(previous_hash)


#: Keys never written to an audit row, even if a caller passes them.
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "password",
        "hashed_password",
        "token",
        "access_token",
        "api_key",
        "secret",
        "authorization",
        "cookie",
        "prompt",
        "evidence_text",
        "content",
        "response_body",
        "signed_url",
    }
)


def _redact(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop content and secrets from audit metadata.

    Audit rows say *what happened to which id*. Raw content, prompts, and
    credentials belong in access-controlled object storage with their own
    retention policy, or nowhere.
    """
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in _FORBIDDEN_METADATA_KEYS:
            cleaned[key] = "[redacted]"
        elif isinstance(value, dict):
            cleaned[key] = _redact(value)
        else:
            cleaned[key] = value
    return cleaned


def verify_chain(events: Sequence[AuditEventRecord]) -> tuple[bool, tuple[str, ...]]:
    """Verify a workspace's audit chain in order."""
    problems: list[str] = []
    previous: str | None = None
    for event in events:
        if event.previous_integrity_hash != previous:
            problems.append(f"event {event.id} does not chain to its predecessor")
        if not event.verify():
            problems.append(f"event {event.id} integrity hash does not match its content")
        previous = event.integrity_hash
    return (not problems), tuple(problems)


def latest_hash(events: Iterable[AuditEventRecord]) -> str | None:
    last: str | None = None
    for event in events:
        last = event.integrity_hash
    return last
