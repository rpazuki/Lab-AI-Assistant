"""Corpus releases, release items, and serving aliases.

Publishing builds new projections and switches an alias atomically. Rollback
switches it back. Neither rewrites history: a correction produces a *successor*
release, and the previous release stays available for reproducing an old answer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import StateTransitionError
from lab_assistant.contracts.release import ReleaseDiff, ReleaseItemRecord, ReleaseManifest
from lab_assistant.domain.states import ReleaseState

__all__ = [
    "CorpusRelease",
    "ReleaseItem",
    "SearchAlias",
    "compute_release_diff",
    "release_transition_allowed",
]


#: The only legal state transitions (data model §13.1).
_ALLOWED_TRANSITIONS: dict[ReleaseState, frozenset[ReleaseState]] = {
    ReleaseState.DRAFT: frozenset({ReleaseState.VALIDATING, ReleaseState.WITHDRAWN}),
    # Passing validation is not approval: a person approves a validated release.
    ReleaseState.VALIDATING: frozenset(
        {ReleaseState.VALIDATED, ReleaseState.REJECTED, ReleaseState.DRAFT}
    ),
    ReleaseState.VALIDATED: frozenset(
        {ReleaseState.APPROVED, ReleaseState.VALIDATING, ReleaseState.WITHDRAWN}
    ),
    ReleaseState.REJECTED: frozenset({ReleaseState.DRAFT, ReleaseState.WITHDRAWN}),
    ReleaseState.APPROVED: frozenset({ReleaseState.PUBLISHING, ReleaseState.REJECTED}),
    ReleaseState.PUBLISHING: frozenset({ReleaseState.PUBLISHED, ReleaseState.APPROVED}),
    ReleaseState.PUBLISHED: frozenset({ReleaseState.SUPERSEDED, ReleaseState.WITHDRAWN}),
    ReleaseState.SUPERSEDED: frozenset({ReleaseState.WITHDRAWN}),
    ReleaseState.WITHDRAWN: frozenset(),
}


def release_transition_allowed(current: ReleaseState, target: ReleaseState) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


class ReleaseItem(BaseModel):
    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    release_id: UUID
    record: ReleaseItemRecord


class CorpusRelease(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    name: str
    semantic_version: str
    status: ReleaseState = ReleaseState.DRAFT
    parent_release_id: UUID | None = None
    scope_version_ids: tuple[UUID, ...] = ()
    policy_version_id: UUID | None = None
    embedding_profile_ids: tuple[str, ...] = ()
    release_manifest_object_id: UUID | None = None
    manifest_sha256: str | None = None
    created_by: UUID | None = None
    approved_by: UUID | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None
    evaluation_run_id: UUID | None = None
    superseded_by_id: UUID | None = None
    #: Gate results recorded at validation time; publish reads these.
    gate_results: dict[str, bool] = Field(default_factory=dict)
    projection_ready_profiles: tuple[str, ...] = ()
    version: int = 1

    def transition_to(self, target: ReleaseState, *, at: datetime | None = None) -> None:
        """Move to ``target`` if the transition table allows it.

        Immutability is about *content*, not about the status field: a published
        release becomes superseded when a successor takes the alias, and its
        manifest, items, and digest are untouched by that. The transition table is
        therefore the whole rule — it already refuses published -> draft.
        """
        if not release_transition_allowed(self.status, target):
            raise StateTransitionError(
                f"release {self.id} cannot move from {self.status.value} to {target.value}",
                context={"release_id": str(self.id)},
            )
        self.status = target
        self.version += 1
        if target is ReleaseState.PUBLISHED:
            self.published_at = at

    def publish_blockers(self) -> tuple[str, ...]:
        """Everything that must hold before the alias may move."""
        blockers: list[str] = []
        if self.status is not ReleaseState.PUBLISHING:
            blockers.append(f"status is {self.status.value}, expected publishing")
        if self.manifest_sha256 is None:
            blockers.append("release manifest has not been hashed")
        if self.approved_by is None:
            blockers.append("no approver recorded")
        if not self.gate_results:
            blockers.append("no gate results recorded")
        failed = [name for name, passed in sorted(self.gate_results.items()) if not passed]
        if failed:
            blockers.append(f"failed gates: {', '.join(failed)}")
        missing_projections = [
            profile
            for profile in self.embedding_profile_ids
            if profile not in self.projection_ready_profiles
        ]
        if missing_projections:
            blockers.append(f"projections not ready: {', '.join(sorted(missing_projections))}")
        return tuple(blockers)


class SearchAlias(BaseModel):
    """The atomic pointer that decides which release serves queries."""

    model_config = {"validate_assignment": True}

    workspace_id: UUID
    alias: str
    release_id: UUID | None = None
    previous_release_id: UUID | None = None
    switched_at: datetime | None = None
    switched_by: UUID | None = None
    version: int = 1

    def switch_to(self, release_id: UUID, *, by: UUID | None, at: datetime | None) -> None:
        self.previous_release_id = self.release_id
        self.release_id = release_id
        self.switched_at = at
        self.switched_by = by
        self.version += 1

    def rollback(self, *, by: UUID | None, at: datetime | None) -> UUID:
        if self.previous_release_id is None:
            raise StateTransitionError(
                f"alias {self.alias} has no previous release to roll back to"
            )
        current = self.release_id
        self.release_id = self.previous_release_id
        self.previous_release_id = current
        self.switched_at = at
        self.switched_by = by
        self.version += 1
        assert self.release_id is not None
        return self.release_id


def compute_release_diff(
    *,
    manifest: ReleaseManifest,
    parent_manifest: ReleaseManifest | None,
    retracted_work_ids: frozenset[UUID] = frozenset(),
    corrected_work_ids: frozenset[UUID] = frozenset(),
    policy_changed: bool = False,
    parser_or_template_changed: bool = False,
) -> ReleaseDiff:
    """Diff by item content digest, which is what makes builds incremental.

    A work whose digest is unchanged has its projection rows copied rather than
    recomputed, so publish time tracks the size of the change and not the size of
    the corpus (scale §5, E10 acceptance).
    """
    if parent_manifest is None:
        return ReleaseDiff(
            parent_release_id=None,
            added_work_ids=tuple(sorted((i.work_id for i in manifest.items), key=str)),
            retracted_work_ids=tuple(sorted(retracted_work_ids, key=str)),
            corrected_work_ids=tuple(sorted(corrected_work_ids, key=str)),
            policy_changed=policy_changed,
            parser_or_template_changed=parser_or_template_changed,
        )

    new_digests = manifest.item_digests()
    old_digests = parent_manifest.item_digests()
    added = sorted((wid for wid in new_digests if wid not in old_digests), key=str)
    removed = sorted((wid for wid in old_digests if wid not in new_digests), key=str)
    changed = sorted(
        (
            wid
            for wid, digest in new_digests.items()
            if wid in old_digests and old_digests[wid] != digest
        ),
        key=str,
    )
    unchanged = sorted(
        (wid for wid, digest in new_digests.items() if old_digests.get(wid) == digest),
        key=str,
    )
    return ReleaseDiff(
        parent_release_id=parent_manifest.release_id,
        added_work_ids=tuple(added),
        removed_work_ids=tuple(removed),
        changed_work_ids=tuple(changed),
        unchanged_work_ids=tuple(unchanged),
        retracted_work_ids=tuple(sorted(retracted_work_ids, key=str)),
        corrected_work_ids=tuple(sorted(corrected_work_ids, key=str)),
        policy_changed=policy_changed,
        parser_or_template_changed=parser_or_template_changed,
    )
