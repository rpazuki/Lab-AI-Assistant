"""What the archive may delete, why the rest stays, and doing it (phase 5).

Physical deletion is the one archive operation that cannot be undone, so it is
two steps with a review between them. ``plan`` reads and decides; ``apply``
recomputes the plan from the same parameters and deletes only if it comes out
byte-identical to the one a person reviewed. An archive that changed in
between — a new publication, a release newly cited — is refused rather than
purged against a picture that is no longer true.

**Reachability, not age.** A payload may be deleted only when no retained
snapshot names it. Snapshots share payloads by design, so dropping an old
snapshot deletes its manifest and shards and only those payloads no retained
snapshot still needs (scenario S40). A snapshot is retained when it is the
head, one of the most recent kept, held by an operator, or the newest snapshot
holding a release some conversation cited: an old chat's citation must still
open (S18).

**Every survivor says why.** A tombstoned payload that a retained snapshot
still names is not deleted, and the plan says which snapshot keeps it and what
would release it (S41), rather than either purging bytes a citation depends on
or saying nothing.

Snapshots are enumerated from the producer's own publication records, not by
listing the bucket: it recorded every snapshot it ever uploaded, including the
abandoned ones whose shards are litter. Only filesystem and memory stores
delete (`ArchiveDeletionStore`); a bucket's deletion authority is a separate
principal with its own lifecycle and retention rules, deliberately not this
process's (ADR-0028).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.archive_catalog import INVENTORY_KIND, MANIFEST_NAME
from lab_assistant.application.archive_control import (
    library_prefix,
    read_catalog_head,
    read_policy_head,
)
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.pagination import every_session
from lab_assistant.application.ports import ArchiveControlStore, ArchiveDeletionStore
from lab_assistant.contracts.archive import ArtifactRef, SnapshotManifest, content_location
from lab_assistant.contracts.archive_policy import PolicyCheckpoint
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.publication import PublicationState
from lab_assistant.domain.installation import InstallationCapability

__all__ = ["RetentionPlan", "RetentionPlanner"]


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    library_id: UUID
    #: The head this plan was computed against. Part of the digest: a plan is
    #: about one state of the archive.
    head_sequence: int | None
    parameters: dict[str, Any]
    #: snapshot id -> why it stays.
    retained: dict[str, list[str]] = field(default_factory=dict)
    #: (key, why) in the order they would be deleted: payloads, then shards,
    #: then each snapshot's manifest last, so an interrupted purge leaves every
    #: surviving manifest still able to name what it lost.
    deletions: tuple[tuple[str, str], ...] = ()
    #: Things that stay although something asked for their removal, and why.
    blocked: tuple[str, ...] = ()

    def digest(self) -> str:
        return sha256_bytes(
            canonical_json(
                {
                    "library_id": self.library_id,
                    "head_sequence": self.head_sequence,
                    "parameters": self.parameters,
                    "retained": self.retained,
                    "deletions": [list(item) for item in self.deletions],
                }
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": str(self.library_id),
            "head_sequence": self.head_sequence,
            "parameters": self.parameters,
            "retained": self.retained,
            "deletions": [{"key": key, "reason": why} for key, why in self.deletions],
            "blocked": list(self.blocked),
            "digest": self.digest(),
        }


class RetentionPlanner:
    def __init__(
        self,
        context: ServiceContext,
        *,
        control: ArchiveControlStore,
        library_id: UUID,
    ) -> None:
        self._context = context
        self._control = control
        self._library_id = library_id
        self._prefix = library_prefix(library_id)

    async def plan(
        self,
        workspace_id: UUID,
        *,
        keep_last: int = 3,
        held_snapshots: tuple[UUID, ...] = (),
        held_digests: tuple[str, ...] = (),
    ) -> RetentionPlan:
        if keep_last < 1:
            raise ValueError("keep_last must keep at least the head")
        parameters = {
            "keep_last": keep_last,
            "held_snapshots": sorted(str(item) for item in held_snapshots),
            "held_digests": sorted(held_digests),
        }
        head, _ = await read_catalog_head(self._control, self._library_id)
        async with self._context.uow_factory() as uow:
            records = await uow.publications.list_for_library(self._library_id)

        published = sorted(
            (record for record in records if record.state.is_visible),
            key=lambda record: record.commit_sequence,
        )
        abandoned = [record for record in records if record.state is PublicationState.FAILED]

        manifests: dict[UUID, SnapshotManifest | None] = {}
        inventories: dict[UUID, list[ArtifactRef]] = {}
        for record in (*published, *abandoned):
            manifest = await self._manifest(record.snapshot_id)
            manifests[record.snapshot_id] = manifest
            inventories[record.snapshot_id] = (
                await self._inventory(record.snapshot_id, manifest) if manifest else []
            )

        retained: dict[UUID, list[str]] = {}

        def keep(snapshot_id: UUID, reason: str) -> None:
            retained.setdefault(snapshot_id, []).append(reason)

        if head is not None:
            keep(head.snapshot_id, "head")
        for record in published[-keep_last:]:
            keep(record.snapshot_id, f"among the {keep_last} most recent")
        for snapshot_id in held_snapshots:
            keep(snapshot_id, "held by an operator")

        blocked: list[str] = []
        for release_id in sorted(await self._cited_releases(workspace_id), key=str):
            if any(
                (manifest := manifests.get(snapshot_id)) is not None
                and release_id in manifest.scientific_release_ids
                for snapshot_id in retained
            ):
                continue
            holder = next(
                (
                    record.snapshot_id
                    for record in reversed(published)
                    if (manifest := manifests.get(record.snapshot_id)) is not None
                    and release_id in manifest.scientific_release_ids
                ),
                None,
            )
            if holder is None:
                blocked.append(
                    f"release {release_id} is cited by a conversation and is in no published "
                    "snapshot this producer recorded; its citations cannot be served from the "
                    "archive"
                )
                continue
            keep(holder, f"holds release {release_id}, which a conversation cited")

        reachable: dict[str, list[UUID]] = {}
        for snapshot_id in retained:
            for ref in inventories.get(snapshot_id, []):
                if ref.is_resolvable:
                    reachable.setdefault(ref.sha256, []).append(snapshot_id)

        payloads: dict[str, str] = {}
        files: list[tuple[str, str]] = []
        manifests_last: list[tuple[str, str]] = []
        for record in (*published, *abandoned):
            snapshot_id = record.snapshot_id
            if snapshot_id in retained:
                continue
            why = (
                "an abandoned upload no head ever named"
                if record.state is PublicationState.FAILED
                else "an older snapshot no retention rule keeps"
            )
            manifest = manifests.get(snapshot_id)
            if manifest is None:
                blocked.append(
                    f"snapshot {snapshot_id} has no readable manifest, so its shards cannot be "
                    "enumerated without listing the bucket; left in place"
                )
                continue
            base = f"{self._prefix}snapshots/{snapshot_id}/"
            for shard in (
                *(shard for ref in manifest.projection_refs for shard in ref.shards),
                *manifest.catalog_shards,
            ):
                files.append((f"{base}{shard.location}", why))
            manifests_last.append((f"{base}{MANIFEST_NAME}", why))
            for ref in inventories.get(snapshot_id, []):
                if not ref.is_resolvable or ref.sha256 in reachable:
                    continue
                if ref.sha256 in held_digests:
                    blocked.append(f"payload {ref.sha256[:12]}… is held by an operator")
                    continue
                payloads[f"{self._prefix}{content_location(ref.sha256)}"] = (
                    "no retained snapshot names it"
                )

        for digest in await self._tombstoned():
            holders = reachable.get(digest)
            if holders:
                blocked.append(
                    f"payload {digest[:12]}… is tombstoned and retained by snapshot(s) "
                    f"{', '.join(sorted(str(h) for h in set(holders)))}; it is purged once "
                    "those are no longer kept"
                )

        deletions = (
            *sorted(payloads.items()),
            *sorted(files),
            *sorted(manifests_last),
        )
        return RetentionPlan(
            library_id=self._library_id,
            head_sequence=head.commit_sequence if head else None,
            parameters=parameters,
            retained={
                str(snapshot_id): sorted(set(reasons))
                for snapshot_id, reasons in sorted(retained.items(), key=lambda kv: str(kv[0]))
            },
            deletions=tuple(deletions),
            blocked=tuple(blocked),
        )

    async def apply(
        self,
        workspace_id: UUID,
        reviewed: dict[str, Any],
        *,
        deletion: ArchiveDeletionStore,
    ) -> dict[str, Any]:
        """Delete what a reviewed plan says, if the archive is still the same."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        parameters = reviewed.get("parameters") or {}
        current = await self.plan(
            workspace_id,
            keep_last=int(parameters.get("keep_last", 3)),
            held_snapshots=tuple(UUID(value) for value in parameters.get("held_snapshots", [])),
            held_digests=tuple(parameters.get("held_digests", [])),
        )
        if current.digest() != reviewed.get("digest"):
            raise LabAssistantError(
                "the archive changed since this plan was reviewed; plan again and review that",
                context={"reviewed": reviewed.get("digest"), "current": current.digest()},
            ).with_class(ErrorClass.CONFLICT)

        removed = 0
        for key, _why in current.deletions:
            removed += bool(await deletion.delete(key))
        async with self._context.uow_factory() as uow:
            await uow.audit.append(
                self._context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action=AuditAction.ARCHIVE_PURGED,
                    subject_type="library",
                    subject_id=self._library_id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    reason_code="retention_plan",
                    metadata={"plan_digest": current.digest(), "deleted": removed},
                )
            )
            await uow.commit()
        return {"deleted": removed, "planned": len(current.deletions), "digest": current.digest()}

    # ── reading ────────────────────────────────────────────────────────────

    async def _manifest(self, snapshot_id: UUID) -> SnapshotManifest | None:
        payload = await self._control.read(f"{self._prefix}snapshots/{snapshot_id}/{MANIFEST_NAME}")
        if payload is None:
            return None
        try:
            return SnapshotManifest.model_validate(json.loads(payload.data))
        except Exception:
            return None

    async def _inventory(self, snapshot_id: UUID, manifest: SnapshotManifest) -> list[ArtifactRef]:
        for shard in manifest.catalog_shards:
            if shard.kind != INVENTORY_KIND:
                continue
            payload = await self._control.read(
                f"{self._prefix}snapshots/{snapshot_id}/{shard.location}"
            )
            if payload is None:
                return []
            return [
                ArtifactRef.model_validate(json.loads(line))
                for line in payload.data.splitlines()
                if line.strip()
            ]
        return []

    async def _cited_releases(self, workspace_id: UUID) -> set[UUID]:
        """Every release a conversation pinned, answered from, or cited."""
        cited: set[UUID] = set()
        async with self._context.uow_factory() as uow:
            for session in await every_session(uow, workspace_id):
                if session.pinned_release_id:
                    cited.add(session.pinned_release_id)
                for message in await uow.chat.list_messages(workspace_id, session.id):
                    if message.release_id:
                        cited.add(message.release_id)
                    cited.update(c.release_id for c in message.citations if c.release_id)
        return cited

    async def _tombstoned(self) -> set[str]:
        head, _ = await read_policy_head(self._control, self._library_id)
        if head is None:
            return set()
        payload = await self._control.read(f"{self._prefix}{head.location}")
        if payload is None:
            return set()
        checkpoint = PolicyCheckpoint.model_validate(json.loads(payload.data))
        return {entry.sha256 for entry in checkpoint.tombstones}
