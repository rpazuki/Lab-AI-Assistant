"""Moving every object from one store to another, verifiably and resumably.

Changing `LAB_OBJECT_STORE_KIND` points the application at an empty bucket. The
evidence stays where it was, every citation stops resolving, and the next write
goes somewhere with no history. That is the failure this exists to prevent, and
it is why the plan says a configuration change is explicitly insufficient.

The shape of the operation:

- **The registry decides what must move.** Every row in `objects`, keyset-paged
  across every workspace, because a migration scoped to one would leave another
  workspace's evidence behind and the shared objects behind every time.
- **The source store is asked what else it holds.** A payload the database does
  not know is usually a transaction that rolled back after its upload. It is
  reported rather than silently left, because "the old bucket is empty now" has
  to be a statement somebody can check.
- **Identity is preserved by construction, not by copying.** An object's id is
  `uuid5(workspace, digest)`, so the destination derives the same id from the
  same bytes. Nothing renames anything; there is no mapping table to get wrong.
- **Creation time travels with the object.** Evidence restamped with the hour it
  was moved has lost something real, so the copy carries the original.
- **Every copy is verified before it counts.** Default is a full read-back and
  digest. That is expensive and it is a one-time operation: the cheap option is
  available and is not the default, because the expensive one is the one that
  catches a truncated transfer.
- **Progress is journalled, so an interrupted run resumes.** A migration of a
  real corpus will be interrupted — by a timeout, a credential expiry, an
  operator. Restarting from zero makes that a disaster rather than an
  inconvenience.

What this does **not** do: it does not switch the application over, and it does
not make the old store read-only. Both are deliberate operator steps, in that
order, after the verification this produces has been read by a person.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.pagination import DEFAULT_PAGE_SIZE, iter_pages
from lab_assistant.application.ports import ObjectStore, StoredObject, SupportsInventory
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.domain.objects import ObjectRecord

__all__ = [
    "MigrationJournal",
    "MigrationPlan",
    "MigrationProblem",
    "MigrationReport",
    "StorageMigrationService",
    "VerifyLevel",
]


class VerifyLevel(StrEnum):
    """How thoroughly a copy is confirmed before it is counted as done."""

    NONE = "none"
    #: The destination knows the object, with the same digest and length. Cheap,
    #: and blind to bytes that arrived damaged under a correct-looking record.
    METADATA = "metadata"
    #: Read the destination back and recompute. The only level that catches a
    #: truncated or substituted transfer, and the default for that reason.
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class MigrationProblem:
    object_id: UUID | None
    kind: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": str(self.object_id) if self.object_id else None,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What a run would do, before it does anything."""

    objects: int = 0
    bytes_total: int = 0
    already_at_destination: int = 0
    bytes_to_copy: int = 0
    tombstoned: int = 0
    #: Payloads the source holds that the registry does not describe.
    source_only: tuple[UUID, ...] = ()
    #: Tombstoned objects, which are not copied. Their denial is recorded in
    #: the database, which a migration does not move, so logical access stays
    #: denied either way. Their bytes remain in the old store, which is why that
    #: store is retained read-only until retention and holds are satisfied —
    #: and why deleting them is a separate, privileged decision.
    tombstoned_not_copied: tuple[UUID, ...] = ()
    #: Registry rows the source store has no record of. Bytes that are gone
    #: from *under* a record the store still has are a different thing, found
    #: when the copy tries to read them: the plan's cheap probe asks the index,
    #: and asking the disk would mean reading the whole corpus twice.
    missing_at_source: tuple[UUID, ...] = ()
    source_enumerable: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "objects": self.objects,
            "bytes_total": self.bytes_total,
            "already_at_destination": self.already_at_destination,
            "bytes_to_copy": self.bytes_to_copy,
            "tombstoned": self.tombstoned,
            "tombstoned_not_copied": [str(value) for value in self.tombstoned_not_copied],
            "source_only": [str(value) for value in self.source_only],
            "missing_at_source": [str(value) for value in self.missing_at_source],
            "source_enumerable": self.source_enumerable,
        }


@dataclass(frozen=True, slots=True)
class MigrationReport:
    run_id: str
    dry_run: bool
    verify: VerifyLevel
    plan: MigrationPlan
    copied: int = 0
    bytes_copied: int = 0
    skipped_present: int = 0
    verified: int = 0
    resumed_from: int = 0
    problems: tuple[MigrationProblem, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "verify": self.verify.value,
            "plan": self.plan.as_dict(),
            "copied": self.copied,
            "bytes_copied": self.bytes_copied,
            "skipped_present": self.skipped_present,
            "verified": self.verified,
            "resumed_from": self.resumed_from,
            "problems": [problem.as_dict() for problem in self.problems],
        }


class MigrationJournal:
    """An append-only record of which objects are done.

    One line per object, flushed and fsynced, because the question it answers is
    asked precisely when the process did not exit cleanly. A journal that lost
    its tail on a crash would send the next run back over work already done —
    harmless but slow — or, worse, make an operator distrust the resume and
    start again from zero.

    It holds object ids and counts. No credentials, no signed URLs, no bytes.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def completed(self) -> set[UUID]:
        if not self._path.is_file():
            return set()
        done: set[UUID] = set()
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line is the normal shape of a crash. Earlier
                    # lines are still good, so the run resumes from them.
                    continue
                if row.get("event") == "copied" and "object_id" in row:
                    done.add(UUID(row["object_id"]))
        return done

    def record(self, event: str, **fields: object) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class StorageMigrationService:
    """Copies the object store, and says exactly what it did."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        destination: ObjectStore,
        source: ObjectStore | None = None,
    ) -> None:
        self._context = context
        self._source = source if source is not None else context.object_store
        self._destination = destination
        if self._source is self._destination:
            raise LabAssistantError(
                "the migration source and destination are the same store",
                context={"hint": "configure a destination that is not the running store"},
            ).with_class(ErrorClass.INVALID_CONTENT)

    async def plan(self, *, page_size: int = DEFAULT_PAGE_SIZE) -> MigrationPlan:
        """Inventory both sides without copying anything."""
        objects = 0
        bytes_total = 0
        present = 0
        bytes_to_copy = 0
        tombstoned = 0
        withheld: list[UUID] = []
        missing: list[UUID] = []
        known: set[UUID] = set()

        async for record in self._registry_records(page_size=page_size):
            objects += 1
            bytes_total += record.byte_length
            known.add(record.id)
            if record.tombstoned:
                tombstoned += 1
                withheld.append(record.id)
                continue
            if await _head(self._source, record.id) is None:
                missing.append(record.id)
                continue
            if await _head(self._destination, record.id) is not None:
                present += 1
            else:
                bytes_to_copy += record.byte_length

        source_only: list[UUID] = []
        enumerable = isinstance(self._source, SupportsInventory)
        if enumerable:
            async for stored in self._source.inventory():  # type: ignore[union-attr]
                if stored.id not in known:
                    source_only.append(stored.id)

        return MigrationPlan(
            objects=objects,
            bytes_total=bytes_total,
            already_at_destination=present,
            bytes_to_copy=bytes_to_copy,
            tombstoned=tombstoned,
            tombstoned_not_copied=tuple(withheld),
            source_only=tuple(source_only),
            missing_at_source=tuple(missing),
            source_enumerable=enumerable,
        )

    async def migrate(
        self,
        *,
        journal: MigrationJournal | None = None,
        dry_run: bool = True,
        verify: VerifyLevel = VerifyLevel.DIGEST,
        include_source_only: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
        run_id: str | None = None,
    ) -> MigrationReport:
        """Copy every object the registry knows, then everything else the
        source holds, verifying each one.

        ``include_source_only`` carries payloads the database does not describe.
        They are usually uploads whose transaction rolled back, and leaving them
        is defensible — but only as a decision somebody made, which is why it is
        a flag rather than a silent omission.
        """
        identifier = run_id or uuid4().hex
        plan = await self.plan(page_size=page_size)
        done = journal.completed() if journal is not None else set()
        report_problems: list[MigrationProblem] = [
            MigrationProblem(
                object_id=object_id,
                kind="missing_at_source",
                detail="the source store cannot produce this object's bytes",
            )
            for object_id in plan.missing_at_source
        ]

        if dry_run:
            return MigrationReport(
                run_id=identifier,
                dry_run=True,
                verify=verify,
                plan=plan,
                copied=(
                    plan.objects
                    - plan.already_at_destination
                    - len(plan.missing_at_source)
                    - len(plan.tombstoned_not_copied)
                ),
                bytes_copied=plan.bytes_to_copy,
                skipped_present=plan.already_at_destination,
                resumed_from=len(done),
                problems=tuple(report_problems),
            )

        if journal is not None:
            journal.record(
                "started",
                run_id=identifier,
                at=_now(self._context),
                objects=plan.objects,
                bytes_total=plan.bytes_total,
            )

        copied = 0
        bytes_copied = 0
        skipped = 0
        verified = 0

        async for record in self._registry_records(page_size=page_size):
            if record.id in done:
                skipped += 1
                continue
            outcome = await self._copy_one(record, verify=verify)
            if isinstance(outcome, MigrationProblem):
                report_problems.append(outcome)
                if journal is not None:
                    journal.record("failed", object_id=str(record.id), detail=outcome.detail)
                continue
            if outcome == "tombstoned":
                continue
            if outcome == "present":
                skipped += 1
            else:
                copied += 1
                bytes_copied += record.byte_length
            if verify is not VerifyLevel.NONE:
                verified += 1
            if journal is not None:
                journal.record("copied", object_id=str(record.id), bytes=record.byte_length)

        if include_source_only and plan.source_only:
            for object_id in plan.source_only:
                if object_id in done:
                    skipped += 1
                    continue
                stored = await _head(self._source, object_id)
                if stored is None:  # pragma: no cover - it was there a moment ago
                    continue
                outcome = await self._copy_one(_as_record(stored), verify=verify)
                if isinstance(outcome, MigrationProblem):
                    report_problems.append(outcome)
                    continue
                if outcome != "present":
                    copied += 1
                    bytes_copied += stored.byte_length
                if journal is not None:
                    journal.record("copied", object_id=str(object_id), bytes=stored.byte_length)

        if journal is not None:
            journal.record(
                "finished",
                run_id=identifier,
                at=_now(self._context),
                copied=copied,
                problems=len(report_problems),
            )

        return MigrationReport(
            run_id=identifier,
            dry_run=False,
            verify=verify,
            plan=plan,
            copied=copied,
            bytes_copied=bytes_copied,
            skipped_present=skipped,
            verified=verified,
            resumed_from=len(done),
            problems=tuple(report_problems),
        )

    # ── internals ──────────────────────────────────────────────────────────

    async def _registry_records(self, *, page_size: int):
        async with self._context.uow_factory() as uow:
            async for record in iter_pages(
                lambda after, size: uow.objects.list_all_objects(limit=size, after_id=after),
                page_size=page_size,
            ):
                yield record

    async def _copy_one(
        self, record: ObjectRecord, *, verify: VerifyLevel
    ) -> str | MigrationProblem:
        if record.tombstoned:
            return "tombstoned"

        existing = await _head(self._destination, record.id)
        if existing is not None:
            if existing.sha256 != record.sha256 or existing.byte_length != record.byte_length:
                return MigrationProblem(
                    object_id=record.id,
                    kind="destination_disagrees",
                    detail=(
                        f"destination holds {existing.sha256[:12]}… of {existing.byte_length} "
                        f"bytes where the registry records {record.sha256[:12]}… of "
                        f"{record.byte_length}"
                    ),
                )
            # Still verified. A second run at --verify digest is the acceptance
            # pass an operator makes before cutover, and skipping what is
            # already there would make it check nothing.
            problem = await self._verify(record, verify)
            return problem if problem is not None else "present"

        try:
            stored = await self._destination.put_stream(
                self._source.stream(record.id),
                workspace_id=record.workspace_id,
                object_class=record.object_class,
                media_type=record.media_type,
                created_at=record.created_at,
            )
        except FileNotFoundError:
            return MigrationProblem(
                object_id=record.id,
                kind="missing_at_source",
                detail="the source store cannot produce this object's bytes",
            )
        except LabAssistantError as exc:
            return MigrationProblem(
                object_id=record.id, kind=exc.error_class.value, detail=exc.detail
            )

        if stored.sha256 != record.sha256:  # pragma: no cover - would be a store defect
            return MigrationProblem(
                object_id=record.id,
                kind="digest_changed",
                detail=f"copied bytes digest to {stored.sha256[:12]}…, not {record.sha256[:12]}…",
            )

        problem = await self._verify(record, verify)
        if problem is not None:
            return problem

        return "copied"

    async def _verify(self, record: ObjectRecord, verify: VerifyLevel) -> MigrationProblem | None:
        if verify is VerifyLevel.NONE:
            return None
        if verify is VerifyLevel.METADATA:
            stored = await _head(self._destination, record.id)
            if stored is None or stored.sha256 != record.sha256:
                return MigrationProblem(
                    object_id=record.id,
                    kind="verification_failed",
                    detail="the destination does not describe the object it was given",
                )
            return None
        try:
            if not await self._destination.verify(record.id):
                return MigrationProblem(
                    object_id=record.id,
                    kind="verification_failed",
                    detail="the destination's bytes do not match the recorded digest",
                )
        except LabAssistantError as exc:
            return MigrationProblem(
                object_id=record.id, kind=exc.error_class.value, detail=exc.detail
            )
        return None


async def _head(store: ObjectStore, object_id: UUID) -> StoredObject | None:
    try:
        return await store.head(object_id)
    except FileNotFoundError:
        return None


def _as_record(stored: StoredObject) -> ObjectRecord:
    return ObjectRecord(
        id=stored.id,
        workspace_id=stored.workspace_id,
        sha256=stored.sha256,
        byte_length=stored.byte_length,
        media_type=stored.media_type,
        object_class=stored.object_class,
        storage_key=stored.key,
        tombstoned=getattr(stored, "tombstoned", False),
        created_at=stored.created_at,
    )


def _now(context: ServiceContext) -> str:
    moment: datetime = context.clock()
    return moment.isoformat()
