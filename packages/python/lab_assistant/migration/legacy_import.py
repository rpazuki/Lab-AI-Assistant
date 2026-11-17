"""Legacy importers (epic E13).

Rules taken directly from the roadmap, and enforced here rather than trusted:

* a legacy datasheet row becomes an **unreviewed proposal**, never an approved
  claim;
* legacy vectors and chunks are **not** imported — projections are regenerated
  from approved renditions;
* PMID and PMCID rows that describe one work are **linked through an identity
  decision**, not merged by string equality;
* every import writes a ``legacy_object_map`` row so old links resolve without
  contaminating new identifiers;
* every run produces an **exception report**; nothing is silently dropped.

Asset classification follows the A–E scheme: A trusted original, B usable with
review, C derived and reproducible, D derived and not fully verifiable, E unsafe
or obsolete. Only A imports cleanly; B is quarantined for review; C is skipped and
regenerated; D imports as an unreviewed proposal with a warning; E never imports.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import AbstentionReason
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.extraction import ProposalSource
from lab_assistant.domain.claims import Claim, ClaimRevision, EpistemicStatus
from lab_assistant.domain.identity import (
    IdentityDecision,
    IdentityDecisionType,
    Work,
    WorkIdentifier,
    WorkKind,
    assess_merge,
    normalise_identifier,
)
from lab_assistant.domain.states import IdentityState, RelevanceState, ReviewState

__all__ = [
    "AssetClass",
    "ImportReport",
    "LegacyImporter",
    "classify_asset",
]

IMPORTER_VERSION = "legacy-import/1"


class AssetClass(StrEnum):
    A_TRUSTED_ORIGINAL = "A"
    B_USABLE_WITH_REVIEW = "B"
    C_DERIVED_REPRODUCIBLE = "C"
    D_DERIVED_UNVERIFIABLE = "D"
    E_UNSAFE_OR_OBSOLETE = "E"

    @property
    def imports_cleanly(self) -> bool:
        return self is AssetClass.A_TRUSTED_ORIGINAL

    @property
    def imports_at_all(self) -> bool:
        return self in (
            AssetClass.A_TRUSTED_ORIGINAL,
            AssetClass.B_USABLE_WITH_REVIEW,
            AssetClass.D_DERIVED_UNVERIFIABLE,
        )


def classify_asset(record: dict[str, Any]) -> tuple[AssetClass, str]:
    """Classify one legacy artefact, with the reason recorded.

    The reason matters: "class B because the acquisition route is unknown" is
    actionable, and "class B" alone is not.
    """
    name = str(record.get("path") or record.get("name") or "")
    if record.get("is_secret") or name.endswith("adminPass"):
        return AssetClass.E_UNSAFE_OR_OBSOLETE, "credential material is never imported"
    if record.get("kind") in ("chunk", "embedding", "vector", "extracted_json"):
        return (
            AssetClass.C_DERIVED_REPRODUCIBLE,
            "derived from a trusted original; regenerated rather than imported",
        )
    if record.get("kind") in ("datasheet_row", "discovery_candidate", "wide_csv"):
        return (
            AssetClass.D_DERIVED_UNVERIFIABLE,
            "post-merge or post-extraction artefact; imports as an unreviewed proposal",
        )
    if not record.get("bytes_available"):
        return AssetClass.E_UNSAFE_OR_OBSOLETE, "no original bytes are available"
    if not record.get("sha256"):
        return AssetClass.B_USABLE_WITH_REVIEW, "checksum was never recorded"
    if not record.get("source_url") or not record.get("licence"):
        return (
            AssetClass.B_USABLE_WITH_REVIEW,
            "acquisition route or licence evidence is incomplete",
        )
    return AssetClass.A_TRUSTED_ORIGINAL, "bytes, checksum, route, and licence all present"


@dataclass
class ImportReport:
    """Counts that must reconcile, plus the exceptions that must have owners."""

    source_rows: int = 0
    works_created: int = 0
    identifiers_linked: int = 0
    identity_conflicts: int = 0
    proposals_created: int = 0
    assets_by_class: Counter[str] = field(default_factory=Counter)
    #: Rows that were not imported, each with a reason.
    skipped: list[dict[str, str]] = field(default_factory=list)
    #: Rows that need a human owner, whether or not they imported. A class-B asset
    #: imported and needs review; an orphan row did not import and needs a
    #: decision. The two lists overlap deliberately.
    exceptions: list[dict[str, str]] = field(default_factory=list)
    mapped: int = 0

    def note_exception(self, *, legacy_id: str, reason: str, imported: bool) -> None:
        """Record an exception, and record the skip when it did not import."""
        self.exceptions.append({"legacy_id": legacy_id, "reason": reason})
        if not imported:
            self.skipped.append({"legacy_id": legacy_id, "reason": reason})

    def reconciles(self) -> bool:
        """Every source row is accounted for exactly once.

        ``exceptions`` annotates rows that *were* imported but need an owner (a
        class-B asset, an orphan row); it is not a third bucket, so counting it
        here would double-count those rows and hide a genuine shortfall.
        """
        return self.mapped + len(self.skipped) == self.source_rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "importer_version": IMPORTER_VERSION,
            "source_rows": self.source_rows,
            "works_created": self.works_created,
            "identifiers_linked": self.identifiers_linked,
            "identity_conflicts": self.identity_conflicts,
            "proposals_created": self.proposals_created,
            "assets_by_class": dict(sorted(self.assets_by_class.items())),
            "mapped": self.mapped,
            "skipped": self.skipped,
            "exceptions": self.exceptions,
            "reconciles": self.reconciles(),
        }

    def to_csv(self) -> str:
        """The downloadable exception list every import run must produce."""
        import io

        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            buffer, fieldnames=["kind", "legacy_id", "reason"], lineterminator="\n"
        )
        writer.writeheader()
        skipped_ids = {row["legacy_id"] for row in self.skipped}
        for row in self.skipped:
            writer.writerow({"kind": "skipped", **row})
        for row in self.exceptions:
            if row["legacy_id"] in skipped_ids:
                continue  # already listed as skipped, with the same reason
            writer.writerow({"kind": "needs_owner", **row})
        return buffer.getvalue()


class LegacyImporter:
    """Imports a legacy snapshot into one workspace, reproducibly."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def import_documents(
        self, actor: ActorContext, *, documents: list[dict[str, Any]]
    ) -> ImportReport:
        """Import legacy ``documents`` rows as works plus identity decisions.

        A legacy row is a *proposal about identity*, not an identity. Two rows with
        the same PMID under different document ids become one work through a
        recorded decision; contradictory strong identifiers become a conflict for a
        human, exactly as a live source disagreement would.
        """
        context = self._context
        report = ImportReport(source_rows=len(documents))

        for row in documents:
            legacy_id = str(row.get("document_id") or row.get("id") or "")
            identifiers = _legacy_identifiers(row)
            if not identifiers:
                report.skipped.append({"legacy_id": legacy_id, "reason": "no usable identifier"})
                continue

            async with context.uow_factory() as uow:
                existing: Work | None = None
                for scheme, value in identifiers:
                    found = await uow.works.find_by_identifier(
                        actor.workspace_id, scheme=scheme.value, value_normalized=value
                    )
                    if found is None:
                        continue
                    if existing is None:
                        existing = found
                        continue
                    if found.id == existing.id:
                        continue
                    assessment = assess_merge(
                        left=existing,
                        right=found,
                        left_identifiers=await uow.works.list_identifiers(
                            actor.workspace_id, existing.id
                        ),
                        right_identifiers=await uow.works.list_identifiers(
                            actor.workspace_id, found.id
                        ),
                    )
                    await uow.works.add_decision(
                        IdentityDecision(
                            id=context.new_id(),
                            workspace_id=actor.workspace_id,
                            decision_type=(
                                IdentityDecisionType.MERGE
                                if assessment.allowed
                                else IdentityDecisionType.REOPEN
                            ),
                            subject_work_ids=(existing.id, found.id),
                            before={"legacy_id": legacy_id},
                            after=assessment.as_decision_payload(),
                            reason_code=f"legacy_import:{assessment.reason_code}",
                            decided_by_type="automatic_rule",
                            decided_at=context.now(),
                        )
                    )
                    if assessment.allowed:
                        found.identity_status = IdentityState.MERGED
                        found.merged_into_work_id = existing.id
                        await uow.works.update(found)
                    else:
                        existing.identity_status = IdentityState.CONFLICT
                        found.identity_status = IdentityState.CONFLICT
                        await uow.works.update(existing)
                        await uow.works.update(found)
                        report.identity_conflicts += 1

                if existing is None:
                    work = Work(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        work_kind=_legacy_work_kind(row),
                        identity_status=IdentityState.UNRESOLVED,
                        # Legacy inclusion is a claim about relevance made by an
                        # older system: it is reviewed, not inherited.
                        relevance=RelevanceState.UNREVIEWED,
                        created_at=context.now(),
                    )
                    await uow.works.add(work)
                    report.works_created += 1
                else:
                    work = existing

                for scheme, value in identifiers:
                    known = {
                        (i.scheme, i.value_normalized)
                        for i in await uow.works.list_identifiers(actor.workspace_id, work.id)
                    }
                    if (scheme, value) in known:
                        continue
                    conflicting = await uow.works.find_by_identifier(
                        actor.workspace_id, scheme=scheme.value, value_normalized=value
                    )
                    if conflicting is not None and conflicting.id != work.id:
                        report.identity_conflicts += 1
                        continue
                    await uow.works.add_identifier(
                        WorkIdentifier(
                            id=context.new_id(),
                            workspace_id=actor.workspace_id,
                            work_id=work.id,
                            scheme=scheme,
                            value_normalized=value,
                            value_display=value,
                            confidence=1.0 if scheme.is_strong else 0.5,
                        )
                    )
                    report.identifiers_linked += 1

                await self._map(
                    uow,
                    actor,
                    legacy_type="document",
                    legacy_id=legacy_id,
                    new_type="work",
                    new_id=work.id,
                    status="imported",
                    note=str(row.get("source") or ""),
                )
                report.mapped += 1
                await uow.commit()

        await self._audit(actor, report, subject="documents")
        return report

    async def import_datasheet_rows(
        self,
        actor: ActorContext,
        *,
        rows: list[dict[str, Any]],
        template_id: str,
        template_version: str,
    ) -> ImportReport:
        """Import legacy datasheet rows as unreviewed claim proposals.

        Every imported value is ``proposal_source = legacy_import``, validation
        status ``invalid`` when it carries no verifiable evidence, and review status
        ``unreviewed``. It can therefore never enter a claim release without a
        reviewer looking at it, which is the point of the whole exercise.
        """
        context = self._context
        report = ImportReport(source_rows=len(rows))

        for row in rows:
            legacy_id = str(row.get("row_id") or row.get("id") or "")
            identifiers = _legacy_identifiers(row)
            if not identifiers:
                report.note_exception(
                    legacy_id=legacy_id,
                    reason="row has no identifier to attach to",
                    imported=False,
                )
                continue
            async with context.uow_factory() as uow:
                work: Work | None = None
                for scheme, value in identifiers:
                    work = await uow.works.find_by_identifier(
                        actor.workspace_id, scheme=scheme.value, value_normalized=value
                    )
                    if work is not None:
                        break
                if work is None:
                    report.note_exception(
                        legacy_id=legacy_id,
                        reason="no imported work matches this row's identifiers",
                        imported=False,
                    )
                    continue

                for predicate, raw_value in (row.get("values") or {}).items():
                    if raw_value in (None, "", "Not reported"):
                        # The legacy "Not reported" conflated several states; it
                        # imports as an explicit unknown rather than as an absence.
                        abstention = AbstentionReason.EXTRACTOR_ABSTAINED
                        verbatim = None
                    else:
                        abstention = AbstentionReason.REPORTED
                        verbatim = str(raw_value)
                    claim = Claim(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        work_id=work.id,
                        predicate=str(predicate),
                        object_kind="text",
                        object_value={"verbatim": verbatim},
                        epistemic_status=EpistemicStatus.UNDETERMINED,
                        template_id=template_id,
                        template_version=template_version,
                        target_id=str(predicate),
                        lifecycle_status="proposed",
                        created_at=context.now(),
                    )
                    revision = ClaimRevision(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        claim_id=claim.id,
                        revision_number=1,
                        value={"verbatim": verbatim, "legacy_quote": row.get("quote")},
                        evidence_span_ids=(),
                        proposal_source=ProposalSource.LEGACY_IMPORT,
                        # No verified span exists: the legacy quote was never
                        # checked against a rendition, so this is not valid evidence.
                        validation_status="invalid",
                        validation_issues=(
                            "legacy_import: evidence was never verified against a rendition",
                        ),
                        review_status=ReviewState.UNREVIEWED,
                        abstention_reason=abstention,
                        epistemic_status=EpistemicStatus.UNDETERMINED,
                        created_at=context.now(),
                    )
                    claim.current_revision_id = revision.id
                    await uow.claims.add_claim(claim)
                    await uow.claims.add_revision(revision)
                    report.proposals_created += 1

                await self._map(
                    uow,
                    actor,
                    legacy_type="datasheet_row",
                    legacy_id=legacy_id,
                    new_type="claim_proposals",
                    new_id=work.id,
                    status="imported_as_proposal",
                    note=f"{template_id}@{template_version}",
                )
                report.mapped += 1
                await uow.commit()

        await self._audit(actor, report, subject="datasheet_rows")
        return report

    async def classify_assets(
        self, actor: ActorContext, *, records: list[dict[str, Any]]
    ) -> ImportReport:
        """Classify a legacy asset inventory A–E and record the reasons."""
        report = ImportReport(source_rows=len(records))
        context = self._context
        for record in records:
            legacy_id = str(record.get("path") or record.get("id") or "")
            asset_class, reason = classify_asset(record)
            report.assets_by_class[asset_class.value] += 1
            if not asset_class.imports_at_all:
                report.skipped.append(
                    {"legacy_id": legacy_id, "reason": f"class {asset_class.value}: {reason}"}
                )
                continue
            if asset_class is not AssetClass.A_TRUSTED_ORIGINAL:
                report.note_exception(
                    legacy_id=legacy_id,
                    reason=f"class {asset_class.value}: {reason}",
                    imported=True,
                )
            async with context.uow_factory() as uow:
                await self._map(
                    uow,
                    actor,
                    legacy_type="asset",
                    legacy_id=legacy_id,
                    new_type="asset_candidate",
                    new_id=None,
                    status=f"class_{asset_class.value}",
                    note=reason,
                )
                report.mapped += 1
                await uow.commit()
        await self._audit(actor, report, subject="assets")
        return report

    def re_acquisition_list(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """What must be fetched again, and at what cost (scale §6).

        Phase 0 needs this number before Phase 3 planning is credible: an unknown
        fraction of the legacy cache is not verifiable and has to be re-fetched at
        policy-compliant rates.
        """
        by_class: Counter[str] = Counter()
        by_host: Counter[str] = Counter()
        unobtainable: list[str] = []
        for record in records:
            asset_class, _reason = classify_asset(record)
            by_class[asset_class.value] += 1
            if asset_class.imports_cleanly or asset_class is AssetClass.C_DERIVED_REPRODUCIBLE:
                continue
            host = str(record.get("host") or record.get("source_url") or "unknown")
            if host == "unknown":
                unobtainable.append(str(record.get("path") or record.get("id")))
            else:
                by_host[_hostname(host)] += 1
        # One request per second per host is a conservative, policy-compliant rate,
        # so the item count is also the wall-clock estimate in seconds.
        wall_clock_seconds = dict(by_host)
        return {
            "counts_by_class": dict(sorted(by_class.items())),
            "re_acquisition_by_host": dict(sorted(by_host.items())),
            "estimated_seconds_by_host": dict(sorted(wall_clock_seconds.items())),
            "unobtainable": sorted(unobtainable),
            "note": (
                "estimates assume one request per second per host; a host with an "
                "approved bulk route should use it instead"
            ),
        }

    @staticmethod
    def load_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    async def _map(
        self,
        uow,
        actor: ActorContext,
        *,
        legacy_type: str,
        legacy_id: str,
        new_type: str,
        new_id: UUID | None,
        status: str,
        note: str,
    ) -> None:
        """Record the legacy-to-new mapping.

        The in-memory adapter has no mapping table, so the row is recorded as an
        audit event there; the SQL adapter writes ``legacy_object_map``. Either way
        the mapping is durable and queryable.
        """
        context = self._context
        await uow.audit.append(
            context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=AuditAction.LEGACY_IMPORT_RUN,
                subject_type=new_type,
                subject_id=new_id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code=status,
                metadata={
                    "legacy_system": "rlalab",
                    "legacy_type": legacy_type,
                    "legacy_id": legacy_id,
                    "legacy_id_digest": sha256_bytes(legacy_id.encode())[:16],
                    "importer_version": IMPORTER_VERSION,
                    "note": note,
                },
            )
        )

    async def _audit(self, actor: ActorContext, report: ImportReport, *, subject: str) -> None:
        context = self._context
        async with context.uow_factory() as uow:
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.LEGACY_IMPORT_RUN,
                    subject_type=f"legacy_{subject}",
                    subject_id=None,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="import_run_complete",
                    metadata=report.as_dict(),
                )
            )
            await uow.commit()


def _legacy_identifiers(row: dict[str, Any]) -> list[tuple[IdentifierScheme, str]]:
    """Extract identifiers from a legacy row, normalising each.

    The predecessor stored the same PMID as ``pmid:*`` and ``pmc:*`` document ids
    depending on the ingestion route. Both are read here, and both become
    identifiers of one work rather than two works.
    """
    candidates: list[tuple[IdentifierScheme, Any]] = [
        (IdentifierScheme.DOI, row.get("doi")),
        (IdentifierScheme.PMID, row.get("pmid")),
        (IdentifierScheme.PMCID, row.get("pmc_id") or row.get("pmcid")),
    ]
    document_id = str(row.get("document_id") or "")
    if document_id.startswith("pmid:"):
        candidates.append((IdentifierScheme.PMID, document_id.removeprefix("pmid:")))
    elif document_id.startswith("pmc:"):
        candidates.append((IdentifierScheme.PMCID, document_id.removeprefix("pmc:")))
    elif document_id.startswith("doi:"):
        candidates.append((IdentifierScheme.DOI, document_id.removeprefix("doi:")))

    out: list[tuple[IdentifierScheme, str]] = []
    seen: set[tuple[str, str]] = set()
    for scheme, raw in candidates:
        if raw in (None, ""):
            continue
        try:
            value = normalise_identifier(scheme, str(raw))
        except ValueError:
            continue
        if (scheme.value, value) in seen:
            continue
        seen.add((scheme.value, value))
        out.append((scheme, value))
    return out


def _legacy_work_kind(row: dict[str, Any]) -> WorkKind:
    source = str(row.get("source") or "").lower()
    kind = str(row.get("work_kind") or "").lower()
    if "preprint" in source or kind == "preprint":
        return WorkKind.PREPRINT
    if "protocol" in source or kind == "protocol":
        return WorkKind.PROTOCOL
    if "inventory" in source or kind == "inventory":
        return WorkKind.INVENTORY
    if source in ("pubmed", "pmc", "crossref", "openalex"):
        return WorkKind.ARTICLE
    return WorkKind.UNKNOWN


def _hostname(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or url
