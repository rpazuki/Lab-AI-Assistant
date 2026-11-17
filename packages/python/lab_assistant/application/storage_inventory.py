"""Reachability between the database and the object store, in both directions.

Two questions, and they are not the same question:

1. Does every row that names an object have a registry row to name? A reference
   to an unregistered object is a foreign key the database would have refused,
   and on a deployment that predates the registry every one of them is in that
   state.
2. Does every registered object still have its bytes? A restore that brought the
   database but not the payloads looks perfectly healthy from SQL.

A sweep that answered only the first would pass on a restore with an empty
bucket. A sweep that answered only the second would pass on the broken graph the
registry was added to fix.

Both walk with a keyset cursor. A `limit` is a page size and never a statement
about how much of a corpus to look at — a sweep that stops at a thousand rows
and reports "verified" is worse than no sweep, because somebody believes it.

Findings are typed rather than formatted, so the CLI decides presentation and the
tests assert on meaning. A finding that blocks is one that makes serving unsafe:
a required reference with nothing behind it, a payload that is gone, bytes that
do not match their digest. An optional reference to a missing log is recorded and
does not stop anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.pagination import DEFAULT_PAGE_SIZE, iter_pages
from lab_assistant.application.ports import ObjectReference, ObjectReferenceSource
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = [
    "BackfillReport",
    "Finding",
    "FindingKind",
    "InventoryReport",
    "PayloadCheck",
    "StorageInventoryService",
]


class FindingKind(StrEnum):
    """What the sweep found, at the granularity the operator's next move needs."""

    #: A row points at an object the registry does not have.
    DANGLING_REFERENCE = "dangling_reference"
    #: The registry has the object; the store does not.
    MISSING_PAYLOAD = "missing_payload"
    #: The store has bytes; they are not the bytes the registry recorded.
    CORRUPT_PAYLOAD = "corrupt_payload"
    #: Storage answered and refused. Not an absence: fix the credential, do not
    #: reacquire the paper.
    DENIED_PAYLOAD = "denied_payload"
    #: Cold storage, restore in progress. Wait; do not re-run the analysis.
    RESTORE_PENDING = "restore_pending"
    #: Storage could not be reached. The sweep is incomplete, not clean.
    UNREADABLE_PAYLOAD = "unreadable_payload"
    #: Registered, and nothing references it. Usually a rolled-back transaction
    #: that left its upload behind. Informational: reclaiming is a separate,
    #: privileged decision that has to account for retention and holds.
    ORPHAN_OBJECT = "orphan_object"
    #: A reference source this store cannot enumerate. The sweep is partial and
    #: says so, rather than counting an unchecked column as checked.
    UNSWEPT_SOURCE = "unswept_source"


#: Findings that make serving unsafe. An orphan does not; an absent optional log
#: does not; a payload that is gone does.
_BLOCKING = frozenset(
    {
        FindingKind.MISSING_PAYLOAD,
        FindingKind.CORRUPT_PAYLOAD,
        FindingKind.DENIED_PAYLOAD,
        FindingKind.UNREADABLE_PAYLOAD,
        FindingKind.UNSWEPT_SOURCE,
    }
)


class PayloadCheck(StrEnum):
    """How hard the sweep looks at the bytes."""

    NONE = "none"
    #: Metadata only: the store knows the object. Cheap, and catches a restore
    #: that brought the database and not the bucket.
    EXISTS = "exists"
    #: Read every byte and recompute the digest. Correct and expensive; this is
    #: the one that catches silent corruption.
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    detail: str
    object_id: UUID | None = None
    source: str | None = None
    row_id: UUID | None = None
    #: True when the reference could not legally be null, so the row is broken
    #: rather than merely incomplete.
    required: bool = False

    @property
    def blocks_serving(self) -> bool:
        if self.kind is FindingKind.DANGLING_REFERENCE:
            return self.required
        return self.kind in _BLOCKING

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "detail": self.detail,
            "object_id": str(self.object_id) if self.object_id else None,
            "source": self.source,
            "row_id": str(self.row_id) if self.row_id else None,
            "required": self.required,
            "blocks_serving": self.blocks_serving,
        }


@dataclass(frozen=True, slots=True)
class InventoryReport:
    sources_swept: tuple[str, ...] = ()
    sources_unswept: tuple[str, ...] = ()
    references_checked: int = 0
    objects_registered: int = 0
    payloads_checked: int = 0
    payload_check: PayloadCheck = PayloadCheck.EXISTS
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.blocks_serving)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, object]:
        return {
            "sources_swept": list(self.sources_swept),
            "sources_unswept": list(self.sources_unswept),
            "references_checked": self.references_checked,
            "objects_registered": self.objects_registered,
            "payloads_checked": self.payloads_checked,
            "payload_check": self.payload_check.value,
            "findings": [finding.as_dict() for finding in self.findings],
            "blocking": len(self.blocking),
        }


@dataclass(frozen=True, slots=True)
class BackfillReport:
    dry_run: bool = True
    dangling_found: int = 0
    registered: int = 0
    #: References whose object the store does not know either. Nothing can
    #: reconstruct these from here: the bytes and their metadata are both gone.
    unrecoverable: tuple[Finding, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "dangling_found": self.dangling_found,
            "registered": self.registered,
            "unrecoverable": [finding.as_dict() for finding in self.unrecoverable],
        }


class StorageInventoryService:
    """Sweeps and repairs the join between the database and the object store."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    # ── reading ────────────────────────────────────────────────────────────

    async def sweep(
        self,
        workspace_id: UUID,
        *,
        payload_check: PayloadCheck = PayloadCheck.EXISTS,
        report_orphans: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> InventoryReport:
        """Walk every reference, then every registered object.

        ``report_orphans`` holds the referenced ids in memory to find registry
        rows nothing points at. That is the one part of this sweep whose cost
        grows with the corpus, so it can be turned off for a very large library
        without giving up the checks that matter for serving.
        """
        findings: list[Finding] = []
        swept: list[str] = []
        unswept: list[str] = []
        referenced: set[UUID] = set()
        references_checked = 0

        async with self._context.uow_factory() as uow:
            for source in uow.objects.reference_sources():
                if not await _enumerable(uow, workspace_id, source):
                    unswept.append(str(source))
                    findings.append(
                        Finding(
                            kind=FindingKind.UNSWEPT_SOURCE,
                            detail=f"{source} cannot be enumerated by this adapter",
                            source=str(source),
                            required=source.required,
                        )
                    )
                    continue
                swept.append(str(source))

                async for reference in iter_pages(
                    _reference_fetch(uow, workspace_id, source), page_size=page_size
                ):
                    references_checked += 1
                    if report_orphans:
                        referenced.add(reference.object_id)
                    if await uow.objects.get(reference.object_id) is None:
                        findings.append(_dangling(reference))

            registered = await uow.objects.count_objects(workspace_id)
            records = [
                record
                async for record in iter_pages(
                    lambda after, size: uow.objects.list_objects(
                        workspace_id, limit=size, after_id=after
                    ),
                    page_size=page_size,
                )
            ]

        payloads_checked = 0
        for record in records:
            if report_orphans and record.id not in referenced and not record.tombstoned:
                findings.append(
                    Finding(
                        kind=FindingKind.ORPHAN_OBJECT,
                        detail=(
                            f"{record.object_class} {record.sha256[:12]}… is registered and "
                            "nothing references it"
                        ),
                        object_id=record.id,
                    )
                )
            if payload_check is PayloadCheck.NONE or record.tombstoned:
                continue
            payloads_checked += 1
            finding = await self._check_payload(record, payload_check)
            if finding is not None:
                findings.append(finding)

        return InventoryReport(
            sources_swept=tuple(swept),
            sources_unswept=tuple(unswept),
            references_checked=references_checked,
            objects_registered=registered,
            payloads_checked=payloads_checked,
            payload_check=payload_check,
            findings=tuple(findings),
        )

    async def _check_payload(self, record, payload_check: PayloadCheck) -> Finding | None:
        store = self._context.object_store
        try:
            # Existence before integrity, always. ``verify`` answers False for an
            # object that is absent and for one whose bytes are wrong, and those
            # are different repairs: one is a restore, the other is a corruption
            # to investigate. Asking the cheaper question first keeps them apart.
            if await store.head(record.id) is None:
                return Finding(
                    kind=FindingKind.MISSING_PAYLOAD,
                    detail=f"{record.object_class} {record.storage_key} is not in the store",
                    object_id=record.id,
                )
            if payload_check is PayloadCheck.DIGEST and not await store.verify(record.id):
                return Finding(
                    kind=FindingKind.CORRUPT_PAYLOAD,
                    detail=(
                        f"stored bytes for {record.object_class} do not match "
                        f"{record.sha256[:12]}…"
                    ),
                    object_id=record.id,
                )
        except LabAssistantError as exc:
            return _from_error(exc, record)
        except FileNotFoundError:
            return Finding(
                kind=FindingKind.MISSING_PAYLOAD,
                detail=f"{record.object_class} {record.storage_key} is not in the store",
                object_id=record.id,
            )
        return None

    # ── repairing ──────────────────────────────────────────────────────────

    async def backfill(
        self,
        workspace_id: UUID,
        *,
        dry_run: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> BackfillReport:
        """Rebuild registry rows for references the store can still account for.

        A deployment that ran before objects were registered has rows pointing
        at payloads that exist, with no registry row between them. The store
        kept its own metadata — digest, length, media type, class — so the row
        can be reconstructed from it exactly, rather than guessed.

        Where the store does not know the object either, nothing here can invent
        it: that is reported, not papered over with a placeholder row that would
        make the next sweep look clean.
        """
        context = self._context
        unrecoverable: list[Finding] = []
        dangling = 0
        registered = 0

        async with context.uow_factory() as uow:
            sources = [
                source
                for source in uow.objects.reference_sources()
                if await _enumerable(uow, workspace_id, source)
            ]

        for source in sources:
            cursor: UUID | None = None
            while True:
                async with context.uow_factory() as uow:
                    page = await uow.objects.list_references(
                        workspace_id, source, limit=page_size, after_id=cursor
                    )
                    if not page:
                        break
                    repaired_here = 0
                    for reference in page:
                        if await uow.objects.get(reference.object_id) is not None:
                            continue
                        dangling += 1
                        stored = await _head(context.object_store, reference.object_id)
                        if stored is None:
                            unrecoverable.append(
                                _dangling(
                                    reference,
                                    detail_suffix=" and the store does not have it either",
                                )
                            )
                            continue
                        if dry_run:
                            registered += 1
                            continue
                        await register_object(uow, stored)
                        repaired_here += 1
                    if repaired_here and not dry_run:
                        await uow.commit()
                        registered += repaired_here
                    cursor = page[-1].row_id
                    if len(page) < page_size:
                        break

        return BackfillReport(
            dry_run=dry_run,
            dangling_found=dangling,
            registered=registered,
            unrecoverable=tuple(unrecoverable),
        )


def _reference_fetch(uow, workspace_id: UUID, source: ObjectReferenceSource):
    async def fetch(after: UUID | None, size: int):
        rows = await uow.objects.list_references(workspace_id, source, limit=size, after_id=after)
        # ``iter_pages`` cursors on ``.id``; a reference's cursor is its row.
        return [_Cursored(row) for row in rows]

    return fetch


@dataclass(frozen=True, slots=True)
class _Cursored:
    """A reference wearing the ``id`` the keyset helper pages on."""

    reference: ObjectReference

    @property
    def id(self) -> UUID:
        return self.reference.row_id

    @property
    def source(self) -> ObjectReferenceSource:
        return self.reference.source

    @property
    def object_id(self) -> UUID:
        return self.reference.object_id

    @property
    def row_id(self) -> UUID:
        return self.reference.row_id


async def _enumerable(uow, workspace_id: UUID, source: ObjectReferenceSource) -> bool:
    try:
        await uow.objects.list_references(workspace_id, source, limit=1)
    except (KeyError, NotImplementedError):
        return False
    return True


async def _head(store, object_id: UUID):
    try:
        return await store.head(object_id)
    except FileNotFoundError:
        return None


def _dangling(reference, *, detail_suffix: str = "") -> Finding:
    return Finding(
        kind=FindingKind.DANGLING_REFERENCE,
        detail=(
            f"{reference.source} row {reference.row_id} points at object "
            f"{reference.object_id}, which is not registered{detail_suffix}"
        ),
        object_id=reference.object_id,
        source=str(reference.source),
        row_id=reference.row_id,
        required=reference.source.required,
    )


def _from_error(exc: LabAssistantError, record) -> Finding:
    """Keep the provider's distinction instead of flattening it to 'missing'."""
    if exc.error_class is ErrorClass.NOT_FOUND:
        kind = FindingKind.MISSING_PAYLOAD
    elif exc.error_class is ErrorClass.STORAGE_DENIED:
        kind = FindingKind.DENIED_PAYLOAD
    elif exc.error_class is ErrorClass.STORAGE_RESTORE_PENDING:
        kind = FindingKind.RESTORE_PENDING
    elif exc.error_class is ErrorClass.INVALID_CONTENT:
        kind = FindingKind.CORRUPT_PAYLOAD
    else:
        kind = FindingKind.UNREADABLE_PAYLOAD
    return Finding(
        kind=kind,
        detail=f"{record.object_class} {record.storage_key}: {exc.detail}",
        object_id=record.id,
    )
