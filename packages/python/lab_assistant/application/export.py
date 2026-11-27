"""Stage 8: reproducible export packages.

An export is a projection of a release. It never changes source records and never
becomes the canonical claim store — the failure mode where a wide CSV was
re-imported as fake document text is structurally impossible here, because a
package is not an ingestion input.

Long-form is canonical and lossless. The wide sheet is a supported, documented
convenience view generated from the same release (a stakeholder commitment, not
an afterthought), and it always carries stable ids back to the long form.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.mirror_access import read_withheld, withheld_work_ids
from lab_assistant.application.objects import register_object
from lab_assistant.application.scope_access import reachable_work_ids
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.errors import NotFound, PolicyDenied
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction, ResourceRef, ResourceType
from lab_assistant.contracts.release import EXPORT_PACKAGE_PROFILE, ExportPackageManifest
from lab_assistant.domain.workspace import Capability

__all__ = [
    "LONG_FORM_COLUMNS",
    "ExportPackage",
    "ExportService",
    "escape_spreadsheet_cell",
    "validate_package",
]

#: The canonical long-form projection (data model §12.2).
LONG_FORM_COLUMNS = (
    "work_id",
    "experiment_id",
    "claim_id",
    "revision_id",
    "subject",
    "predicate",
    "value_verbatim",
    "value_normalised",
    "unit",
    "basis",
    "qualifiers",
    "evidence_span_id",
    "source_locator",
    "review_status",
    "abstention_reason",
    "template_version",
)

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_spreadsheet_cell(value: Any) -> str:
    """Neutralise spreadsheet formula injection on export.

    A cell beginning ``=``, ``+``, ``-``, or ``@`` is prefixed with an apostrophe.
    Research documents are untrusted input, and a claim value can come from one.
    """
    text = "" if value is None else str(value)
    if text.startswith(_DANGEROUS_PREFIXES):
        return f"'{text}"
    return text


@dataclass(frozen=True, slots=True)
class ExportPackage:
    """An in-memory package: path -> bytes, plus its manifest."""

    files: dict[str, bytes]
    manifest: ExportPackageManifest

    @property
    def payload_paths(self) -> tuple[str, ...]:
        return tuple(sorted(path for path in self.files if path.startswith("data/")))

    def to_object_bytes(self) -> bytes:
        """Serialise the package deterministically for object storage."""
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.files):
                # Fixed timestamp keeps the archive byte-identical across builds.
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, self.files[path])
        return buffer.getvalue()


class ExportService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def build(
        self,
        actor: ActorContext,
        *,
        release_id: UUID,
        purpose: str = "internal_research",
        include_wide: bool = True,
        include_assets: bool = False,
    ) -> ExportPackage:
        """Build a checksummed, self-describing package for one release."""
        actor.require(Capability.EXPORT)
        context = self._context
        policy_input = actor.policy_input(now=context.now())
        if not context.policy_engine.allows(
            policy_input,
            action=PolicyAction.EXPORT_CLAIMS,
            resource=ResourceRef(resource_type=ResourceType.RELEASE, resource_id=release_id),
        ):
            raise PolicyDenied("you may not export this release")

        may_export_evidence = context.policy_engine.allows(
            policy_input,
            action=PolicyAction.EXPORT_EVIDENCE,
            resource=ResourceRef(resource_type=ResourceType.RELEASE, resource_id=release_id),
        )

        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            withheld = await read_withheld(uow, actor.workspace_id)
            if release is None or release.id in withheld.release_ids:
                # A release this mirror removed is absent here, as for Ask.
                raise NotFound(f"release {release_id} not found")
            items = await uow.releases.list_items(actor.workspace_id, release_id)
            # A release may have been assembled by someone who reaches more scopes
            # than the person exporting it, so its papers are filtered again here
            # rather than trusted. Skipped ones are counted like any other
            # restricted paper, so the package says how much it is missing.
            reachable = await reachable_work_ids(uow, context, actor)
            removed = await withheld_work_ids(uow, actor.workspace_id, withheld)

            works: list[dict[str, Any]] = []
            manifestations: list[dict[str, Any]] = []
            entities: list[dict[str, Any]] = []
            claims: list[dict[str, Any]] = []
            evidence: list[dict[str, Any]] = []
            relations: list[dict[str, Any]] = []
            long_form: list[dict[str, Any]] = []
            rights_summary: list[dict[str, Any]] = []
            skipped_restricted = 0

            from lab_assistant.application.reconciliation import canonical_metadata

            for item in items:
                record = item.record
                work = await uow.works.get(actor.workspace_id, record.work_id)
                if work is None:
                    continue
                if (reachable is not None and work.id not in reachable) or work.id in removed:
                    skipped_restricted += 1
                    continue
                if not context.policy_engine.allows(
                    policy_input,
                    action=PolicyAction.EXPORT_METADATA,
                    resource=ResourceRef(resource_type=ResourceType.WORK, resource_id=work.id),
                ):
                    skipped_restricted += 1
                    continue
                assertions = await uow.observations.list_assertions(
                    actor.workspace_id, work_id=work.id
                )
                metadata = canonical_metadata(list(assertions))
                identifiers = await uow.works.list_identifiers(actor.workspace_id, work.id)
                works.append(
                    {
                        "work_id": str(work.id),
                        "work_kind": work.work_kind.value,
                        "identity_status": work.identity_status.value,
                        "relevance": work.relevance.value,
                        "retraction_status": work.retraction_status,
                        "title": metadata.get("title"),
                        "year": metadata.get("year"),
                        "journal": metadata.get("journal"),
                        "identifiers": [
                            {"scheme": i.scheme.value, "value": i.value_display}
                            for i in identifiers
                        ],
                    }
                )
                for relation in await uow.works.list_relations(actor.workspace_id, work.id):
                    relations.append(
                        {
                            "from_work_id": str(relation.from_work_id),
                            "to_work_id": str(relation.to_work_id),
                            "kind": relation.kind.value,
                        }
                    )
                if record.manifestation_id:
                    manifestation = await uow.assets.get_manifestation(
                        actor.workspace_id, record.manifestation_id
                    )
                    if manifestation is not None:
                        manifestations.append(
                            {
                                "manifestation_id": str(manifestation.id),
                                "work_id": str(work.id),
                                "kind": manifestation.manifestation_kind.value,
                                "declared_mime": manifestation.declared_mime,
                                "asset_id": str(record.asset_id) if record.asset_id else None,
                            }
                        )
                if record.asset_id:
                    asset = await uow.assets.get_asset(actor.workspace_id, record.asset_id)
                    if asset is not None and asset.rights_decision_id:
                        decision = await uow.policies.get_rights_decision(
                            actor.workspace_id, asset.rights_decision_id
                        )
                        if decision is not None:
                            rights_summary.append(
                                {
                                    "work_id": str(work.id),
                                    "asset_sha256": asset.sha256,
                                    "licence": decision.licence_expression,
                                    "access_basis": decision.access_basis,
                                    "state": decision.status.value,
                                    "restrictions": "; ".join(decision.restrictions),
                                    "allowed_actions": ", ".join(
                                        sorted(a.value for a in decision.allowed_actions)
                                    ),
                                }
                            )

                for revision_id in record.claim_revision_ids:
                    revision = await uow.claims.get_revision(actor.workspace_id, revision_id)
                    if revision is None:
                        continue
                    claim = await uow.claims.get_claim(actor.workspace_id, revision.claim_id)
                    if claim is None:
                        continue
                    claims.append(
                        {
                            "claim_id": str(claim.id),
                            "revision_id": str(revision.id),
                            "work_id": str(claim.work_id),
                            "experiment_id": (
                                str(claim.experiment_id) if claim.experiment_id else None
                            ),
                            "predicate": claim.predicate,
                            "subject": claim.subject_label,
                            "object_kind": claim.object_kind,
                            "value": revision.value,
                            "review_status": revision.review_status.value,
                            "abstention_reason": revision.abstention_reason.value,
                            "epistemic_status": revision.epistemic_status.value,
                            "template": f"{claim.template_id}@{claim.template_version}",
                        }
                    )
                    span_ids: list[str] = []
                    locators: list[str] = []
                    for span_id in revision.evidence_span_ids:
                        span = await uow.renditions.get_span(actor.workspace_id, span_id)
                        if span is None:
                            continue
                        span_ids.append(str(span.id))
                        locators.append(json.dumps(span.locator, sort_keys=True))
                        if may_export_evidence:
                            evidence.append(
                                {
                                    "evidence_span_id": str(span.id),
                                    "rendition_id": str(span.rendition_id),
                                    "node_id": span.node_id,
                                    "start_offset": span.start_offset,
                                    "end_offset": span.end_offset,
                                    "quote_sha256": span.quote_sha256,
                                    "quote_preview": span.quote_preview,
                                    "normalisation_version": span.normalisation_version,
                                    "locator": span.locator,
                                }
                            )
                        else:
                            evidence.append(
                                {
                                    "evidence_span_id": str(span.id),
                                    "rendition_id": str(span.rendition_id),
                                    "node_id": span.node_id,
                                    "quote_sha256": span.quote_sha256,
                                    "quote_preview": "[redacted: export_evidence not permitted]",
                                    "normalisation_version": span.normalisation_version,
                                }
                            )
                    long_form.append(
                        {
                            "work_id": str(claim.work_id),
                            "experiment_id": (
                                str(claim.experiment_id) if claim.experiment_id else ""
                            ),
                            "claim_id": str(claim.id),
                            "revision_id": str(revision.id),
                            "subject": claim.subject_label or "",
                            "predicate": claim.predicate,
                            "value_verbatim": revision.value.get("verbatim") or "",
                            "value_normalised": revision.value.get("normalised") or "",
                            "unit": revision.value.get("unit_canonical")
                            or revision.value.get("unit_verbatim")
                            or "",
                            "basis": revision.value.get("basis") or "",
                            "qualifiers": json.dumps(claim.qualifiers, sort_keys=True),
                            "evidence_span_id": ";".join(span_ids),
                            "source_locator": ";".join(locators),
                            "review_status": revision.review_status.value,
                            "abstention_reason": revision.abstention_reason.value,
                            "template_version": f"{claim.template_id}@{claim.template_version}",
                        }
                    )
                    if claim.subject_entity_id:
                        entity = None  # entities are optional in the first release
                        if entity is not None:  # pragma: no cover - placeholder branch
                            entities.append(entity)

        files: dict[str, bytes] = {}
        files["data/works.jsonl"] = _jsonl(works)
        files["data/manifestations.jsonl"] = _jsonl(manifestations)
        files["data/claims.jsonl"] = _jsonl(claims)
        files["data/evidence.jsonl"] = _jsonl(evidence)
        files["data/entities.jsonl"] = _jsonl(entities)
        files["data/relations.jsonl"] = _jsonl(relations)
        files["data/long-form.csv"] = _csv(long_form, LONG_FORM_COLUMNS)
        if include_wide:
            files["data/wide-form.csv"] = _wide_csv(long_form)
            wide_xlsx = _maybe_xlsx(long_form)
            if wide_xlsx is not None:
                files["data/wide-form.xlsx"] = wide_xlsx
        files["assets-manifest.csv"] = _csv(
            [
                {
                    "work_id": row["work_id"],
                    "asset_sha256": row["asset_sha256"],
                    "included": "false",
                    "licence": row["licence"] or "",
                }
                for row in rights_summary
            ],
            ("work_id", "asset_sha256", "included", "licence"),
        )
        files["metadata/rights-summary.csv"] = _csv(
            rights_summary,
            (
                "work_id",
                "asset_sha256",
                "licence",
                "access_basis",
                "state",
                "restrictions",
                "allowed_actions",
            ),
        )

        payload_digests = {
            path: sha256_bytes(content)
            for path, content in files.items()
            if path.startswith("data/") or path == "assets-manifest.csv"
        }
        manifest = ExportPackageManifest(
            profile=EXPORT_PACKAGE_PROFILE,
            release_id=release_id,
            workspace_id=actor.workspace_id,
            created_at=context.now(),
            exported_by=actor.user_id,
            purpose=purpose,
            payload_digests=payload_digests,
            policy_version_id=release.policy_version_id,
            rights_summary=tuple(rights_summary),
            assets_included=include_assets,
            row_counts={
                "works": len(works),
                "manifestations": len(manifestations),
                "claims": len(claims),
                "evidence": len(evidence),
                "long_form": len(long_form),
                "skipped_restricted_works": skipped_restricted,
            },
        )

        files["metadata/release.json"] = canonical_json(
            {
                "release_id": str(release_id),
                "name": release.name,
                "semantic_version": release.semantic_version,
                "manifest_sha256": release.manifest_sha256,
                "status": release.status.value,
                "published_at": release.published_at,
                "parent_release_id": (
                    str(release.parent_release_id) if release.parent_release_id else None
                ),
                "embedding_profiles": list(release.embedding_profile_ids),
                "gate_results": release.gate_results,
            }
        )
        files["metadata/schema.json"] = canonical_json(_schema_document())
        files["metadata/provenance.jsonl"] = _jsonl(
            [
                {
                    "entity": "corpus_release",
                    "id": str(release_id),
                    "activity": "export",
                    "agent": str(actor.user_id),
                    "at": context.now().isoformat(),
                    "profile": EXPORT_PACKAGE_PROFILE,
                }
            ]
        )
        files["metadata/validation-report.json"] = canonical_json(
            {"gates": release.gate_results, "row_counts": manifest.row_counts}
        )
        files["ro-crate-metadata.json"] = canonical_json(_ro_crate(manifest, release.name))
        files["bagit.txt"] = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
        files["bag-info.txt"] = (
            f"Bag-Software-Agent: lab-ai-assistant\n"
            f"External-Identifier: release:{release_id}\n"
            f"Bagging-Date: {context.now().date().isoformat()}\n"
            f"Profile: {EXPORT_PACKAGE_PROFILE}\n"
        ).encode()
        files["README.md"] = _readme(manifest, release.name).encode()
        files["manifest-sha256.txt"] = manifest.bagit_manifest_text().encode()

        tag_digests = {
            path: sha256_bytes(content)
            for path, content in files.items()
            if path.startswith("metadata/")
            or path in ("ro-crate-metadata.json", "bagit.txt", "bag-info.txt", "README.md")
        }
        manifest = manifest.model_copy(update={"tag_digests": tag_digests})
        files["tagmanifest-sha256.txt"] = manifest.tagmanifest_text().encode()

        package = ExportPackage(files=files, manifest=manifest)

        stored = await context.object_store.put(
            package.to_object_bytes(),
            workspace_id=actor.workspace_id,
            object_class="export_package",
            media_type="application/zip",
        )
        async with context.uow_factory() as uow:
            await register_object(uow, stored)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.EXPORT_CREATED,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=purpose,
                    metadata={
                        "object_id": str(stored.id),
                        "sha256": stored.sha256,
                        "row_counts": manifest.row_counts,
                        "evidence_exported": may_export_evidence,
                        "profile": EXPORT_PACKAGE_PROFILE,
                    },
                )
            )
            await uow.commit()
        return package


def validate_package(package: ExportPackage) -> tuple[bool, tuple[str, ...]]:
    """Validate a package offline: checksums, required files, schema presence."""
    problems: list[str] = []
    required = (
        "README.md",
        "bagit.txt",
        "bag-info.txt",
        "manifest-sha256.txt",
        "tagmanifest-sha256.txt",
        "metadata/release.json",
        "metadata/schema.json",
        "metadata/provenance.jsonl",
        "metadata/rights-summary.csv",
        "metadata/validation-report.json",
        "ro-crate-metadata.json",
        "data/works.jsonl",
        "data/claims.jsonl",
        "data/evidence.jsonl",
        "data/long-form.csv",
    )
    for path in required:
        if path not in package.files:
            problems.append(f"missing required file {path}")

    for path, digest in package.manifest.payload_digests.items():
        content = package.files.get(path)
        if content is None:
            problems.append(f"manifest lists {path} but the package does not contain it")
            continue
        if sha256_bytes(content) != digest:
            problems.append(f"checksum mismatch for {path}")
    for path, digest in package.manifest.tag_digests.items():
        content = package.files.get(path)
        if content is None:
            problems.append(f"tagmanifest lists {path} but the package does not contain it")
        elif sha256_bytes(content) != digest:
            problems.append(f"tag checksum mismatch for {path}")

    long_form = package.files.get("data/long-form.csv", b"").decode("utf-8")
    if long_form:
        reader = csv.DictReader(io.StringIO(long_form))
        if tuple(reader.fieldnames or ()) != LONG_FORM_COLUMNS:
            problems.append("long-form column set does not match the documented schema")
        for row in reader:
            if not row.get("revision_id"):
                problems.append("long-form row without a revision id")
                break
    return (not problems), tuple(problems)


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    lines = [canonical_json(row).decode("utf-8") for row in rows]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _csv(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: escape_spreadsheet_cell(row.get(key)) for key in columns})
    return buffer.getvalue().encode("utf-8")


def _wide_csv(long_form: list[dict[str, Any]]) -> bytes:
    """One row per (work, experiment), one column per predicate.

    Multiple values for the same predicate in one experiment are joined with
    ``" | "`` and the cell records how many there were, so a multi-experiment work
    cannot silently lose a measurement in the wide view.
    """
    predicates = sorted({row["predicate"] for row in long_form})
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for row in long_form:
        key = (row["work_id"], row["experiment_id"])
        bucket = grouped.setdefault(key, {})
        value = row["value_verbatim"] or row["abstention_reason"]
        unit = row["unit"]
        bucket.setdefault(row["predicate"], []).append(
            f"{value} {unit}".strip() if unit else str(value)
        )
        bucket.setdefault("_revision_ids", []).append(row["revision_id"])

    columns = ("work_id", "experiment_id", "revision_ids", *predicates)
    rows: list[dict[str, Any]] = []
    for (work_id, experiment_id), bucket in sorted(grouped.items()):
        row: dict[str, Any] = {
            "work_id": work_id,
            "experiment_id": experiment_id,
            "revision_ids": ";".join(bucket.get("_revision_ids", [])),
        }
        for predicate in predicates:
            values = bucket.get(predicate, [])
            if not values:
                row[predicate] = ""
            elif len(values) == 1:
                row[predicate] = values[0]
            else:
                row[predicate] = f"{' | '.join(values)} ({len(values)} values)"
        rows.append(row)
    return _csv(rows, columns)


def wide_rows(long_form: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    """The wide sheet as columns and rows, exactly as ``_wide_csv`` writes it."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(_wide_csv(long_form).decode("utf-8")))
    return list(reader.fieldnames or []), list(reader)


def _maybe_xlsx(long_form: list[dict[str, Any]]) -> bytes | None:
    """Wide XLSX when openpyxl is installed; the CSV is always present."""
    try:
        from openpyxl import Workbook
    except ModuleNotFoundError:
        return None
    wide = _wide_csv(long_form).decode("utf-8")
    reader = csv.reader(io.StringIO(wide))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "datasheet"
    for row in reader:
        sheet.append([escape_spreadsheet_cell(cell) for cell in row])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _schema_document() -> dict[str, Any]:
    return {
        "profile": EXPORT_PACKAGE_PROFILE,
        "files": {
            "data/works.jsonl": "one JSON object per canonical work",
            "data/manifestations.jsonl": "one object per manifestation in the release",
            "data/claims.jsonl": "one object per published claim revision",
            "data/evidence.jsonl": "one object per evidence span referenced by a claim",
            "data/relations.jsonl": "work-to-work relations (version, preprint, supplement)",
            "data/long-form.csv": "canonical lossless projection; one row per claim revision",
            "data/wide-form.csv": "convenience view; one row per work and experiment",
            "data/wide-form.xlsx": "same as wide-form.csv, when openpyxl is available",
        },
        "long_form_columns": list(LONG_FORM_COLUMNS),
        "conventions": {
            "encoding": "UTF-8",
            "csv_dialect": "excel with LF line endings",
            "formula_injection": "cells beginning = + - @ are prefixed with an apostrophe",
            "absent_values": (
                "an empty cell means the projection has no value; an explicit "
                "abstention_reason means the source was searched and the value was absent. "
                "They are never interchangeable."
            ),
            "multiple_values": (
                "the wide view joins multiple values with ' | ' and states the count; "
                "the long form always has one row per value"
            ),
        },
    }


def _ro_crate(manifest: ExportPackageManifest, release_name: str) -> dict[str, Any]:
    return {
        "@context": "https://w3id.org/ro/crate/1.1/context",
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
                "about": {"@id": "./"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": f"Lab AI Assistant export: {release_name}",
                "identifier": f"release:{manifest.release_id}",
                "datePublished": (manifest.created_at.isoformat() if manifest.created_at else None),
                "description": (
                    "Long-form claim and evidence projection of an immutable corpus release. "
                    f"Declared profile: {manifest.profile}. Strict conformance is not claimed."
                ),
                "hasPart": [{"@id": path} for path in sorted(manifest.payload_digests)],
            },
            *[
                {"@id": path, "@type": "File", "sha256": digest}
                for path, digest in sorted(manifest.payload_digests.items())
            ],
        ],
    }


def _readme(manifest: ExportPackageManifest, release_name: str) -> str:
    counts = "\n".join(f"- {key}: {value}" for key, value in sorted(manifest.row_counts.items()))
    return f"""# Export package: {release_name}

Release: `{manifest.release_id}`
Profile: {manifest.profile}
Created: {manifest.created_at.isoformat() if manifest.created_at else "unknown"}
Purpose: {manifest.purpose}

## What this is

A projection of one immutable corpus release. `data/long-form.csv` is canonical:
one row per claim revision, with the evidence span and source locator that
support it. `data/wide-form.csv` and `data/wide-form.xlsx` are convenience views
of the same rows and carry `revision_ids` back to the long form.

This package is an output. It is not an input: re-importing it as document text
would destroy the scientific structure it was built to preserve.

## Validating offline

1. recompute SHA-256 for every path in `manifest-sha256.txt`;
2. recompute SHA-256 for every path in `tagmanifest-sha256.txt`;
3. check `data/long-form.csv` headers against `metadata/schema.json`.

`tools/package-validator` performs all three.

## Row counts

{counts}

## Rights

`metadata/rights-summary.csv` records the licence, access basis, and permitted
actions for every asset behind these claims. Evidence previews are redacted where
the exporting user did not hold `export_evidence`.
"""
