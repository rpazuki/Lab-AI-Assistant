"""Moving a library's head to a snapshot, exactly once (phase 2).

Publishing is two operations that must never be confused: uploading the
snapshot, which is slow and safely repeatable, and moving the head, which is
instant and must happen once. The whole protocol is arranged so that every
crash lands on a side of that line where the answer is obvious.

The order, and what a fault at each point means:

1. **validate** the snapshot locally. Nothing has been touched.
2. **check the head** and decide whether this publisher may advance it
   (`domain/publication.py`). A refusal here is the end — notably, a fenced
   writer must *not* re-read and retry.
3. **record the intent** in the outbox, with the head version it read. A crash
   now leaves a `prepared` row and nothing else.
4. **upload** the snapshot under its own id. A crash now leaves objects nobody
   references: a reader resolves the head and never sees them. Litter, not a
   publication, and the retention sweep's problem rather than the library's.
5. **mark verified** once the uploaded copy validates where it landed.
6. **swap the head**, conditional on the version read at step 2. This is the
   only moment anything becomes visible.
7. **record the commit**, then acknowledge.

A crash between 6 and 7 is the one that needs the outbox: the snapshot is live
and the producer does not know it. Republishing would claim a second sequence
for one piece of work, so recovery reads the head, recognises its own snapshot,
and writes down what already happened. The head is always the authority — a
local row claiming a commit the head does not carry did not commit.

Nothing here retries a failed swap. That is not an oversight: a swap fails
because somebody else moved the head, and if their epoch is higher this process
has been fenced by a deliberate handover. Re-reading and retrying with the
epoch it just observed is a fenced writer taking the library back, and it would
work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from lab_assistant.application.archive_catalog import MANIFEST_NAME
from lab_assistant.application.archive_control import library_prefix, read_policy_head
from lab_assistant.application.archive_validate import validate_snapshot
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.ports import ArchiveControlStore
from lab_assistant.contracts.archive import SnapshotManifest
from lab_assistant.contracts.archive_policy import policy_location
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.publication import (
    HEAD_LOCATION,
    HeadDescriptor,
    PublicationRecord,
    PublicationState,
)
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.publication import (
    CommitRefusal,
    CommitVerdict,
    ReconcileAction,
    ReconcileVerdict,
    authority_refusal,
    evaluate_commit,
    next_head,
    reconcile,
)

__all__ = ["PublicationReport", "PublicationService", "library_prefix"]


@dataclass(frozen=True, slots=True)
class PublicationReport:
    snapshot_id: UUID
    state: PublicationState | None = None
    verdict: CommitVerdict | None = None
    head: HeadDescriptor | None = None
    record_id: UUID | None = None
    uploaded: int = 0
    problems: tuple[str, ...] = field(default_factory=tuple)
    next_steps: tuple[str, ...] = ()

    @property
    def published(self) -> bool:
        return self.state is not None and self.state.is_visible

    @property
    def ok(self) -> bool:
        return not self.problems and (self.verdict is None or self.verdict.permitted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": str(self.snapshot_id),
            "state": self.state.value if self.state else None,
            "published": self.published,
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "head": {
                "commit_sequence": self.head.commit_sequence,
                "snapshot_id": str(self.head.snapshot_id),
                "writer_epoch": self.head.writer_epoch,
                "owner_installation_id": str(self.head.owner_installation_id),
            }
            if self.head
            else None,
            "record_id": str(self.record_id) if self.record_id else None,
            "uploaded": self.uploaded,
            "problems": list(self.problems),
            "next_steps": list(self.next_steps),
        }


class PublicationService:
    """Prepares, uploads and commits a snapshot — and recovers an interrupted one."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        control: ArchiveControlStore,
        library_id: UUID,
        installation_id: UUID,
        writer_epoch: int = 0,
        verifier: Any | None = None,
        require_signature: bool = False,
    ) -> None:
        self._context = context
        self._control = control
        self._library_id = library_id
        self._installation_id = installation_id
        self._writer_epoch = writer_epoch
        #: The keys this deployment trusts, so a snapshot is checked for who
        #: signed it before it is uploaded rather than after a reader refuses it.
        self._verifier = verifier
        self._require_signature = require_signature

    # ── the head ───────────────────────────────────────────────────────────

    def _head_key(self) -> str:
        return f"{library_prefix(self._library_id)}{HEAD_LOCATION}"

    async def read_head(self) -> tuple[HeadDescriptor | None, str | None]:
        """The current head and its version token, or ``(None, None)``.

        ``None`` means the library is genuinely empty. A denial or a timeout
        raises instead: a publisher that read "no head" from a permissions
        error would conclude the library is empty and create one over the top
        of a library it simply could not see.
        """
        import json

        payload = await self._control.read(self._head_key())
        if payload is None:
            return None, None
        try:
            head = HeadDescriptor.model_validate(json.loads(payload.data))
        except Exception as exc:
            raise LabAssistantError(
                "the library head does not parse",
                context={"key": self._head_key(), "detail": str(exc)},
            ).with_class(ErrorClass.INVALID_CONTENT) from exc
        return head, payload.version

    # ── the protocol ───────────────────────────────────────────────────────

    async def publish(
        self, workspace_id: UUID, snapshot_directory: Path, *, dry_run: bool = False
    ) -> PublicationReport:
        """Validate, check the head, upload, and swap. In that order."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        snapshot_directory = Path(snapshot_directory)
        manifest, problems = self._load(snapshot_directory)
        if manifest is None:
            return PublicationReport(snapshot_id=uuid4(), problems=tuple(problems))

        head, version = await self.read_head()
        verdict = evaluate_commit(
            head,
            library_id=self._library_id,
            owner_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            snapshot_id=manifest.snapshot_id,
        )
        if verdict.permitted:
            verdict = await self._check_policy_head(manifest) or verdict
        if not verdict.permitted:
            return PublicationReport(
                snapshot_id=manifest.snapshot_id,
                verdict=verdict,
                head=head,
                problems=(verdict.detail or "this publisher may not advance the head",),
                next_steps=_refusal_steps(verdict),
            )

        if dry_run:
            return PublicationReport(
                snapshot_id=manifest.snapshot_id,
                verdict=verdict,
                head=head,
                next_steps=("re-run without --dry-run to publish",),
            )

        record = await self._prepare(workspace_id, manifest, verdict, version)
        record, uploaded = await self._upload(record, snapshot_directory, manifest)
        record, head = await self._commit(record, manifest, head, version)

        return PublicationReport(
            snapshot_id=manifest.snapshot_id,
            state=record.state,
            verdict=verdict,
            head=head,
            record_id=record.id,
            uploaded=uploaded,
            next_steps=(
                f"the head is at sequence {head.commit_sequence}; readers will see this "
                "snapshot on their next refresh",
            ),
        )

    async def _prepare(
        self,
        workspace_id: UUID,
        manifest: SnapshotManifest,
        verdict: CommitVerdict,
        version: str | None,
    ) -> PublicationRecord:
        """Record the intent before anything moves.

        The head version read a moment ago is stored with it: the swap is
        against *that*, so a head that moves underneath fails the precondition
        instead of being overwritten.
        """
        existing = await self._find(manifest.snapshot_id)
        if existing is not None and not existing.state.is_terminal:
            return existing

        record = PublicationRecord(
            id=self._context.new_id(),
            workspace_id=workspace_id,
            library_id=self._library_id,
            snapshot_id=manifest.snapshot_id,
            snapshot_manifest_digest=_manifest_digest(manifest),
            state=PublicationState.PREPARED,
            writer_epoch=self._writer_epoch,
            commit_sequence=verdict.commit_sequence,
            expected_head_version=version,
            created_at=self._context.now(),
        )
        async with self._context.uow_factory() as uow:
            await uow.publications.add(record)
            await uow.commit()
        return record

    async def _upload(
        self, record: PublicationRecord, directory: Path, manifest: SnapshotManifest
    ) -> tuple[PublicationRecord, int]:
        """Put the snapshot in the archive, under its own id.

        Create-only on every key: a snapshot id names one immutable snapshot,
        and a second write under the same key would be somebody rewriting
        history. A key that already holds the identical bytes is not an error —
        that is a resumed upload, which is the common case after a crash.
        """
        prefix = f"{library_prefix(self._library_id)}snapshots/{record.snapshot_id}/"
        uploaded = 0

        for relative in (MANIFEST_NAME, *(shard.location for shard in manifest.catalog_shards)):
            source = directory / relative
            if not source.is_file():
                raise LabAssistantError(
                    "the snapshot is missing a file its manifest names",
                    context={"file": relative, "directory": str(directory)},
                ).with_class(ErrorClass.INVALID_CONTENT)
            data = source.read_bytes()
            try:
                await self._control.create(f"{prefix}{relative}", data)
                uploaded += 1
            except LabAssistantError as exc:
                if exc.error_class is not ErrorClass.CONFLICT:
                    raise
                # Already there. A resumed upload after a crash, which is the
                # case this step is arranged to make cheap.

        verified = record.model_copy(
            update={"state": PublicationState.VERIFIED, "updated_at": self._context.now()}
        )
        await self._save(verified)
        return verified, uploaded

    async def _commit(
        self,
        record: PublicationRecord,
        manifest: SnapshotManifest,
        head: HeadDescriptor | None,
        version: str | None,
    ) -> tuple[PublicationRecord, HeadDescriptor]:
        """The swap. The only moment any of this becomes visible."""
        descriptor = next_head(
            head,
            library_id=self._library_id,
            owner_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            snapshot_id=record.snapshot_id,
            snapshot_manifest_digest=record.snapshot_manifest_digest,
            commit_sequence=record.commit_sequence,
            updated_at=self._context.now(),
        )
        payload = descriptor.serialise()
        key = self._head_key()

        try:
            if head is None or version is None:
                committed_version = await self._control.create(key, payload)
            else:
                committed_version = await self._control.compare_and_swap(
                    key, payload, expected_version=version
                )
        except LabAssistantError as exc:
            # Deliberately not retried. See the module docstring: a failed swap
            # can mean a handover fenced this writer, and adopting the epoch it
            # would now observe is how a fenced publisher takes the library
            # back.
            failed = record.model_copy(
                update={
                    "state": PublicationState.FAILED,
                    "detail": f"the head moved underneath this attempt: {exc.detail}",
                    "updated_at": self._context.now(),
                }
            )
            await self._save(failed)
            raise

        committed = record.model_copy(
            update={
                "state": PublicationState.COMMITTED,
                "committed_head_version": committed_version,
                "updated_at": self._context.now(),
            }
        )
        await self._save(committed)

        acknowledged = committed.model_copy(
            update={"state": PublicationState.ACKNOWLEDGED, "updated_at": self._context.now()}
        )
        await self._save(acknowledged)
        return acknowledged, descriptor

    # ── recovery ───────────────────────────────────────────────────────────

    async def reconcile(self, *, apply: bool = False) -> list[ReconcileVerdict]:
        """What every interrupted attempt means, and optionally act on it.

        Reads the head once and judges each open attempt against it. The head
        is the authority: an attempt that believes it committed while the head
        says otherwise did not commit.
        """
        if apply:
            self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        head, _version = await self.read_head()
        async with self._context.uow_factory() as uow:
            open_attempts = await uow.publications.list_open(self._library_id)

        verdicts = [reconcile(record, head) for record in open_attempts]
        if not apply:
            return verdicts

        by_id = {record.id: record for record in open_attempts}
        for verdict in verdicts:
            record = by_id.get(verdict.record_id) if verdict.record_id else None
            if record is None:
                continue
            if verdict.action is ReconcileAction.ACKNOWLEDGE:
                await self._save(
                    record.model_copy(
                        update={
                            "state": PublicationState.ACKNOWLEDGED,
                            "detail": verdict.detail,
                            "updated_at": self._context.now(),
                        }
                    )
                )
            elif verdict.action is ReconcileAction.ABANDON:
                await self._save(
                    record.model_copy(
                        update={
                            "state": PublicationState.FAILED,
                            "detail": verdict.detail,
                            "updated_at": self._context.now(),
                        }
                    )
                )
        return verdicts

    # ── internals ──────────────────────────────────────────────────────────

    async def _check_policy_head(self, manifest: SnapshotManifest) -> CommitVerdict | None:
        """The policy head beside the catalog head: fenced together, and bound.

        Fenced together, because a handover moves the two heads one after the
        other and may stop between them; whichever it reached first must fence
        this writer from both (`authority_refusal`). And bound, because a
        snapshot names the checkpoint that was current when it was built, and a
        reader will fetch exactly that one. A snapshot naming a checkpoint the
        archive does not hold would be published unreadable.
        """
        policy_head, _ = await read_policy_head(self._control, self._library_id)
        refused = authority_refusal(
            policy_head,
            library_id=self._library_id,
            owner_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
        )
        if refused is not None:
            return CommitVerdict(
                permitted=False,
                refusal=refused.refusal,
                detail=f"the policy head refuses this writer: {refused.detail}",
            )

        if manifest.policy_sequence == 0:
            return None
        if policy_head is None or policy_head.policy_sequence < manifest.policy_sequence:
            return CommitVerdict(
                permitted=False,
                refusal=CommitRefusal.POLICY_UNRESOLVED,
                detail=(
                    f"the snapshot was built under policy sequence {manifest.policy_sequence} "
                    "and the library's policy head has not reached it: a head that went "
                    "backwards is a restored or replayed archive, not a publication target"
                ),
            )
        key = (
            f"{library_prefix(self._library_id)}"
            f"{policy_location(manifest.policy_sequence, manifest.policy_artifact_digest or '')}"
        )
        if await self._control.read(key) is None:
            return CommitVerdict(
                permitted=False,
                refusal=CommitRefusal.POLICY_UNRESOLVED,
                detail=(
                    f"the snapshot names policy checkpoint {manifest.policy_sequence} "
                    f"({(manifest.policy_artifact_digest or '')[:12]}…), which the archive "
                    "does not hold: a reader could not fetch the policy it was built under"
                ),
            )
        return None

    def _load(self, directory: Path) -> tuple[SnapshotManifest | None, list[str]]:
        """Validate before anything else. A snapshot that does not validate
        locally must not reach the archive, where fixing it is expensive.

        The signature is checked too, against this deployment's trusted keys,
        so a snapshot a reader would refuse is refused here first.
        """
        report = validate_snapshot(
            directory, verifier=self._verifier, require_signature=self._require_signature
        )
        if report.manifest is None or report.blocking:
            return None, [
                "the snapshot does not validate, so it was not uploaded",
                *[finding.detail for finding in report.blocking],
            ]
        return report.manifest, []

    async def _find(self, snapshot_id: UUID) -> PublicationRecord | None:
        async with self._context.uow_factory() as uow:
            return await uow.publications.find_by_snapshot(self._library_id, snapshot_id)

    async def _save(self, record: PublicationRecord) -> None:
        async with self._context.uow_factory() as uow:
            await uow.publications.update(record)
            await uow.commit()


def _refusal_steps(verdict: CommitVerdict) -> tuple[str, ...]:
    from lab_assistant.domain.publication import CommitRefusal

    if verdict.refusal is CommitRefusal.FENCED_BY_EPOCH:
        return (
            "this installation has been fenced by a handover. Stop publishing here.",
            "do NOT re-run with the epoch the head reported: that is the fenced writer "
            "taking the library back, and it would succeed",
        )
    if verdict.refusal is CommitRefusal.ALREADY_PUBLISHED:
        return ("nothing to do: the head already carries this snapshot",)
    if verdict.refusal is CommitRefusal.FOREIGN_OWNER:
        return (
            "two installations claim this library. Establish which is the publisher "
            "before either publishes again",
        )
    if verdict.refusal is CommitRefusal.POLICY_UNRESOLVED:
        return (
            "issue the policy checkpoint first (`lab archive policy publish --apply`), then "
            "package the snapshot again so it names a checkpoint readers can fetch",
        )
    return ("resolve the refusal before retrying",)


def _manifest_digest(manifest: SnapshotManifest) -> str:
    """What the head records: the digest a signature covers.

    Excludes the signature itself, so signing a snapshot does not change which
    snapshot it is. An unsigned snapshot and the same snapshot signed have one
    identity, and a head written before signing still names the right thing.
    """
    return manifest.signing_digest()
