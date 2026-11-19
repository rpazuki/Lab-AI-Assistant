"""Import many PDFs at once into the acquisitions waiting for them.

The predecessor had upload folders. Here a batch of files is matched to the
acquisition plans that are waiting for a person (status ``assisted`` or
``failed``), and each match goes through exactly the path a single assisted
upload takes: the same rights decision, integrity and malware checks, identity
check, and parse task. A batch is a convenience, not a second, looser door.

Matching is by strong identifier only — DOI, PMID or PMCID — and never by
title. Identifiers are looked for, in order:

1. the file name (``10.1234_abcd.pdf``, ``PMID12345678.pdf``, ``PMC9876543.pdf``);
2. the PDF's document metadata and its **first page**.

Only the first page is read, deliberately: the reference list of any paper is
full of other papers' DOIs, and a match on one of those would attach the wrong
document to a work. A file whose identifiers point at more than one waiting
plan is reported as ambiguous and left for a person, never guessed.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from lab_assistant.application.acquisition import AcquisitionService
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import LabAssistantError, ValidationFailure
from lab_assistant.domain.identity import IdentifierScheme, normalise_identifier
from lab_assistant.domain.workspace import Capability

__all__ = [
    "BulkFile",
    "BulkImportService",
    "FileOutcome",
    "FileResult",
    "identifiers_in_name",
    "identifiers_in_text",
]

#: Plans a person is expected to complete.
WAITING_PLAN_STATES = frozenset({"assisted", "failed"})

#: The most files one batch takes. A folder bigger than this is several batches,
#: so one bad file never holds hundreds of good ones hostage in one request.
MAX_FILES = 200
MAX_FILE_BYTES = 100 * 1024 * 1024

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`\[\]]+)", re.IGNORECASE)
_PMID = re.compile(r"\bPMID[\s:_-]*(\d{4,9})\b", re.IGNORECASE)
_PMCID = re.compile(r"\b(PMC\d{4,9})\b", re.IGNORECASE)
_TRAILING = ".,;:)]}'\""


class FileOutcome(StrEnum):
    IMPORTED = "imported"
    #: The upload was accepted but scanning did not pass; a steward must resolve it.
    BLOCKED = "blocked"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    #: Matched and refused by a check (rights, integrity, identity, size, type).
    REJECTED = "rejected"
    #: Matched, and would be imported; only reported by a dry run.
    MATCHED = "matched"


@dataclass(frozen=True, slots=True)
class BulkFile:
    filename: str
    content: bytes
    media_type: str | None = None


@dataclass(slots=True)
class FileResult:
    filename: str
    outcome: FileOutcome
    identifiers: list[str] = field(default_factory=list)
    plan_id: UUID | None = None
    work_id: UUID | None = None
    asset_id: UUID | None = None
    parse_task_id: UUID | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "outcome": self.outcome.value,
            "identifiers": self.identifiers,
            "plan_id": str(self.plan_id) if self.plan_id else None,
            "work_id": str(self.work_id) if self.work_id else None,
            "asset_id": str(self.asset_id) if self.asset_id else None,
            "parse_task_id": str(self.parse_task_id) if self.parse_task_id else None,
            "detail": self.detail,
        }


def _keys(scheme: IdentifierScheme, values: Sequence[str]) -> list[str]:
    keys: list[str] = []
    for value in values:
        try:
            key = f"{scheme.value}:{normalise_identifier(scheme, value.rstrip(_TRAILING))}"
        except ValueError:
            continue
        if key not in keys:
            keys.append(key)
    return keys


def identifiers_in_text(text: str) -> list[str]:
    """Identifier keys (``doi:…``, ``pmid:…``, ``pmcid:…``) found in a text."""
    return [
        *_keys(IdentifierScheme.DOI, _DOI.findall(text)),
        *_keys(IdentifierScheme.PMID, _PMID.findall(text)),
        *_keys(IdentifierScheme.PMCID, _PMCID.findall(text)),
    ]


def identifiers_in_name(filename: str) -> list[str]:
    """Identifier keys in a file name.

    A DOI cannot contain ``/`` in a file name, so the usual substitutes (``_`` or
    ``%2F`` after the registrant prefix) are read back as the slash.
    """
    stem = filename.rsplit("/", 1)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE).replace("%2F", "/").replace("%2f", "/")
    doi = re.match(r"^(?:doi[_:\s-]*)?(10\.\d{4,9})[_/](.+)$", stem, re.IGNORECASE)
    keys = _keys(IdentifierScheme.DOI, [f"{doi.group(1)}/{doi.group(2)}"]) if doi else []
    bare_pmid = re.fullmatch(r"(?:pmid[_:\s-]*)?(\d{6,9})", stem, re.IGNORECASE)
    keys += _keys(IdentifierScheme.PMID, [bare_pmid.group(1)] if bare_pmid else [])
    keys += _keys(IdentifierScheme.PMCID, _PMCID.findall(stem))
    return keys


def _first_page_identifiers(content: bytes) -> list[str]:
    """Identifiers in the PDF's metadata and first page. Empty if unreadable."""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - the parsers extra is absent
        return []
    try:
        reader = PdfReader(io.BytesIO(content))
        metadata: Any = reader.metadata or {}
        texts = [str(value) for value in metadata.values() if isinstance(value, str)]
        if reader.pages:
            texts.append(reader.pages[0].extract_text() or "")
    except Exception:
        # A file pypdf cannot open still gets its name read, and the upload checks
        # report what is wrong with it if it matches.
        return []
    return identifiers_in_text("\n".join(texts))


class BulkImportService:
    def __init__(self, context: ServiceContext, orchestrator: Any | None = None) -> None:
        self._context = context
        self._orchestrator = orchestrator

    async def waiting_plans(self, actor: ActorContext) -> dict[str, list[tuple[UUID, UUID, UUID]]]:
        """Identifier key -> [(plan id, work id, manifestation id)] for waiting plans."""
        index: dict[str, list[tuple[UUID, UUID, UUID]]] = {}
        async with self._context.uow_factory() as uow:
            plans = await uow.assets.list_plans(actor.workspace_id)
            for plan in plans:
                if plan.status not in WAITING_PLAN_STATES or not plan.manifestation_ids:
                    continue
                for identifier in await uow.works.list_identifiers(
                    actor.workspace_id, plan.work_id
                ):
                    if identifier.scheme not in (
                        IdentifierScheme.DOI,
                        IdentifierScheme.PMID,
                        IdentifierScheme.PMCID,
                    ):
                        continue
                    key = f"{identifier.scheme.value}:{identifier.value_normalized}"
                    entry = (plan.id, plan.work_id, plan.manifestation_ids[0])
                    if entry not in index.setdefault(key, []):
                        index[key].append(entry)
        return index

    async def run(
        self,
        actor: ActorContext,
        *,
        files: Sequence[BulkFile],
        access_basis: str,
        attestation: str,
        dry_run: bool = False,
    ) -> list[FileResult]:
        """Match each file and, unless ``dry_run``, import every unambiguous match."""
        actor.require(Capability.UPLOAD_ASSET)
        if not files:
            raise ValidationFailure("no files were given")
        if len(files) > MAX_FILES:
            raise ValidationFailure(f"at most {MAX_FILES} files per batch; split the folder")
        if not dry_run and not attestation.strip():
            raise ValidationFailure("an attestation is required to import")
        index = await self.waiting_plans(actor)
        claimed: set[UUID] = set()
        results: list[FileResult] = []
        for item in files:
            result = await self._one(
                actor,
                item,
                index=index,
                claimed=claimed,
                access_basis=access_basis,
                attestation=attestation,
                dry_run=dry_run,
            )
            results.append(result)
        return results

    async def _one(
        self,
        actor: ActorContext,
        item: BulkFile,
        *,
        index: dict[str, list[tuple[UUID, UUID, UUID]]],
        claimed: set[UUID],
        access_basis: str,
        attestation: str,
        dry_run: bool,
    ) -> FileResult:
        if len(item.content) > MAX_FILE_BYTES:
            return FileResult(item.filename, FileOutcome.REJECTED, detail="larger than 100 MB")
        keys = identifiers_in_name(item.filename)
        matches = {entry for key in keys for entry in index.get(key, [])}
        if not matches:
            # Only opened when the name says nothing useful: reading every PDF of a
            # large batch is the slow part, and a name match is the stronger signal.
            keys = [*keys, *(k for k in _first_page_identifiers(item.content) if k not in keys)]
            matches = {entry for key in keys for entry in index.get(key, [])}
        if not matches:
            return FileResult(
                item.filename,
                FileOutcome.UNMATCHED,
                identifiers=keys,
                detail=(
                    "no waiting acquisition has these identifiers"
                    if keys
                    else "no DOI, PMID or PMCID in the file name or first page"
                ),
            )
        plans = {plan_id for plan_id, _, _ in matches}
        if len(plans) > 1:
            return FileResult(
                item.filename,
                FileOutcome.AMBIGUOUS,
                identifiers=keys,
                detail=f"matches {len(plans)} waiting acquisitions; assign it by hand",
            )
        plan_id, work_id, manifestation_id = next(iter(matches))
        if plan_id in claimed:
            return FileResult(
                item.filename,
                FileOutcome.AMBIGUOUS,
                identifiers=keys,
                plan_id=plan_id,
                work_id=work_id,
                detail="another file in this batch already matched the same acquisition",
            )
        claimed.add(plan_id)
        if dry_run:
            return FileResult(
                item.filename,
                FileOutcome.MATCHED,
                identifiers=keys,
                plan_id=plan_id,
                work_id=work_id,
            )
        try:
            asset = await AcquisitionService(self._context).register_assisted_upload(
                actor,
                plan_id=plan_id,
                manifestation_id=manifestation_id,
                content=item.content,
                filename=item.filename,
                access_basis=access_basis,
                attestation=attestation,
                declared_media_type=item.media_type or "application/pdf",
            )
        except LabAssistantError as exc:
            return FileResult(
                item.filename,
                FileOutcome.REJECTED,
                identifiers=keys,
                plan_id=plan_id,
                work_id=work_id,
                detail=exc.detail,
            )
        blocked, task_id = await self._queue_parse(actor, asset)
        return FileResult(
            item.filename,
            FileOutcome.BLOCKED if blocked else FileOutcome.IMPORTED,
            identifiers=keys,
            plan_id=plan_id,
            work_id=work_id,
            asset_id=asset.id,
            parse_task_id=task_id,
            detail=None
            if not blocked
            else f"scanning did not pass ({asset.malware_scan_status.value}); a steward must "
            "resolve this before the document is parsed",
        )

    async def _queue_parse(self, actor: ActorContext, asset: Any) -> tuple[bool, UUID | None]:
        """Queue parsing exactly when the single-upload route would.

        Returns whether scanning blocked the file, and the parse task if one was
        queued (none without an orchestrator, as in a dry service test).
        """
        from lab_assistant.contracts.stage import StageName
        from lab_assistant.domain.assets import MalwareScanStatus

        scan_required = self._context.settings.malware_scan_required_for_uploads
        if asset.malware_scan_status is not MalwareScanStatus.PASSED and scan_required:
            return True, None
        if self._orchestrator is None:
            return False, None
        task = await self._orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.PARSING,
            inputs={"asset_id": str(asset.id)},
        )
        return False, task.id
