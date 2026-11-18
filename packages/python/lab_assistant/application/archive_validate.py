"""Checking a snapshot without the producer's database (phase 2).

This is the acceptance gate phase 2 names first: *the archive fixture can be
validated without the producer DB*. Everything here reads the snapshot
directory and nothing else — no database, no object store, no network — because
the question it answers is the importer's question, and an importer has none of
those things.

What it establishes, in the order a reader would need it:

1. **The manifest is the manifest.** It parses, it digests to what the file
   beside it records, and its format major is one this code reads.
2. **Every shard is the shard the manifest describes.** Present, the declared
   length, the declared digest, the declared row count. A shard that grew a row
   in transit is caught here rather than by a foreign key halfway through an
   import.
3. **Every row is a row of its kind.** Parsed against the record model for the
   shard's kind. A row that will not parse is a finding, not a traceback, and
   one bad row does not hide every row after it.
4. **The closure closes.** Every reference a row makes to another row resolves
   *within the snapshot*. This is the check that distinguishes an archive from
   a table dump: a release entry naming a work that did not travel is an
   archive that cannot be served, and the producer's database is exactly what
   is not available to notice it later.

Severity is a decision about the importer, not a feeling about the finding.
**Blocking** means a reader that imported this would serve something wrong or
incomplete. **Advisory** means it would serve correctly with something an
operator should still know. An unknown shard kind is advisory — a newer
producer may have added one and the format's minor-version rule says to
preserve what you do not understand — but an unresolvable *required* reference
is blocking, because there is no reading of it that serves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from lab_assistant.application.archive_catalog import (
    INVENTORY_KIND,
    MANIFEST_NAME,
    PROJECTION_CHUNK_KIND,
    PROJECTION_VECTOR_KIND,
)
from lab_assistant.contracts.archive import ArtifactRef, SnapshotManifest
from lab_assistant.contracts.archive_catalog import RecordKind, record_for
from lab_assistant.contracts.archive_projection import (
    VECTOR_ENCODING,
    ProjectionChunkRecord,
    ProjectionVectorRecord,
    vector_problems,
)
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.signing import statement_for
from lab_assistant.domain.signing import evaluate_signature

__all__ = [
    "Severity",
    "ValidationFinding",
    "ValidationReport",
    "validate_snapshot",
]


class Severity(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    severity: Severity
    detail: str

    @property
    def blocks(self) -> bool:
        return self.severity is Severity.BLOCKING

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ValidationReport:
    directory: str
    manifest: SnapshotManifest | None = None
    counts: dict[str, int] = field(default_factory=dict)
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocks)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.blocking

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "snapshot_id": str(self.manifest.snapshot_id) if self.manifest else None,
            "format_version": self.manifest.format_version if self.manifest else None,
            "counts": dict(sorted(self.counts.items())),
            "findings": [finding.as_dict() for finding in self.findings],
            "blocking": len(self.blocking),
            "valid": self.ok,
        }


#: Which shard a reference points into, per record kind and field. Declared as
#: data rather than as code so a new edge is one line and cannot be forgotten
#: in a branch somebody did not read.
_EDGES: dict[RecordKind, tuple[tuple[str, RecordKind, bool], ...]] = {
    RecordKind.MANIFESTATION: (("work_id", RecordKind.WORK, True),),
    RecordKind.ASSET: (
        ("work_id", RecordKind.WORK, False),
        ("manifestation_id", RecordKind.MANIFESTATION, False),
        ("rights_decision_id", RecordKind.RIGHTS_DECISION, True),
    ),
    RecordKind.RENDITION: (
        ("asset_id", RecordKind.ASSET, True),
        ("work_id", RecordKind.WORK, False),
        ("supersedes_rendition_id", RecordKind.RENDITION, False),
    ),
    RecordKind.SPAN: (("rendition_id", RecordKind.RENDITION, True),),
    RecordKind.CLAIM: (("work_id", RecordKind.WORK, True),),
    RecordKind.CLAIM_REVISION: (
        ("claim_id", RecordKind.CLAIM, True),
        # Blocking: a release pins a revision, and the revision it replaced is
        # the reason the number is what it is. A truncated chain is a corpus
        # that cannot explain its own correction.
        ("supersedes_revision_id", RecordKind.CLAIM_REVISION, True),
    ),
    RecordKind.REVIEW: (("revision_id", RecordKind.CLAIM_REVISION, True),),
    RecordKind.RELEASE_ITEM: (
        ("release_id", RecordKind.RELEASE, True),
        ("work_id", RecordKind.WORK, True),
        ("manifestation_id", RecordKind.MANIFESTATION, False),
        ("asset_id", RecordKind.ASSET, True),
        ("rendition_id", RecordKind.RENDITION, True),
    ),
}

#: List-valued references, checked per element.
_LIST_EDGES: dict[RecordKind, tuple[tuple[str, RecordKind, bool], ...]] = {
    RecordKind.CLAIM_REVISION: (("evidence_span_ids", RecordKind.SPAN, True),),
    RecordKind.RELEASE_ITEM: (("claim_revision_ids", RecordKind.CLAIM_REVISION, True),),
    RecordKind.RELEASE: (("scope_version_ids", RecordKind.SCOPE_VERSION, True),),
}


def validate_snapshot(
    directory: Path,
    *,
    verifier: Any | None = None,
    require_signature: bool = False,
) -> ValidationReport:
    """Validate a snapshot directory. Touches nothing but the directory.

    ``verifier`` carries the keys this deployment was configured to trust.
    Without one the signature is reported and not checked, which is honest:
    "nobody told me whose key to expect" is not "this is fine".
    """
    directory = Path(directory)
    findings: list[ValidationFinding] = []

    manifest = _read_manifest(directory, findings)
    if manifest is None:
        return ValidationReport(directory=str(directory), findings=tuple(findings))

    _check_format(manifest, findings)
    rows, counts = _read_shards(directory, manifest, findings)
    _check_closure(rows, findings)
    _check_inventory(directory, manifest, rows, findings)
    _check_releases(manifest, rows, findings)
    counts |= _check_projections(directory, manifest, rows, findings)
    _check_signature(manifest, findings, verifier=verifier, require=require_signature)

    return ValidationReport(
        directory=str(directory),
        manifest=manifest,
        counts=counts,
        findings=tuple(findings),
    )


# ── the manifest ───────────────────────────────────────────────────────────


def _read_manifest(directory: Path, findings: list[ValidationFinding]) -> SnapshotManifest | None:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        findings.append(
            ValidationFinding(
                code="manifest_absent",
                severity=Severity.BLOCKING,
                detail=f"{MANIFEST_NAME} is not here: this directory is not a snapshot",
            )
        )
        return None
    payload = path.read_bytes()
    try:
        manifest = SnapshotManifest.model_validate(json.loads(payload))
    except Exception as exc:
        # The finding, not a traceback: one unparseable manifest must not hide
        # the fact that it is the manifest which is wrong.
        findings.append(
            ValidationFinding(
                code="manifest_unparseable",
                severity=Severity.BLOCKING,
                detail=f"{MANIFEST_NAME} does not parse ({exc})",
            )
        )
        return None

    recorded = directory / "MANIFEST.sha256"
    if recorded.is_file():
        expected = recorded.read_text(encoding="utf-8").split()[0]
        actual = sha256_bytes(payload)
        if expected != actual:
            findings.append(
                ValidationFinding(
                    code="manifest_edited",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"the manifest digests to {actual[:12]}… and MANIFEST.sha256 "
                        f"records {expected[:12]}…: it was changed after it was written"
                    ),
                )
            )
    else:
        findings.append(
            ValidationFinding(
                code="manifest_digest_absent",
                severity=Severity.ADVISORY,
                detail="MANIFEST.sha256 is absent, so an edited manifest cannot be detected",
            )
        )
    return manifest


def _check_format(manifest: SnapshotManifest, findings: list[ValidationFinding]) -> None:
    major = manifest.format_version.split(".")[0]
    if major != "1":
        findings.append(
            ValidationFinding(
                code="format_unsupported",
                severity=Severity.BLOCKING,
                detail=(
                    f"snapshot format {manifest.format_version} is not a 1.x this code "
                    "reads; an unknown major changes meaning, not just fields"
                ),
            )
        )
    elif manifest.format_version != "1.0":
        findings.append(
            ValidationFinding(
                code="format_newer_minor",
                severity=Severity.ADVISORY,
                detail=(
                    f"snapshot format {manifest.format_version} is a newer minor than 1.0; "
                    "fields this version does not know are preserved, not interpreted"
                ),
            )
        )


# ── shards ─────────────────────────────────────────────────────────────────


def _read_shards(
    directory: Path, manifest: SnapshotManifest, findings: list[ValidationFinding]
) -> tuple[dict[RecordKind, dict[UUID, Any]], dict[str, int]]:
    rows: dict[RecordKind, dict[UUID, Any]] = {}
    counts: dict[str, int] = {}

    for shard in manifest.catalog_shards:
        path = directory / shard.location
        if not path.is_file():
            findings.append(
                ValidationFinding(
                    code="shard_absent",
                    severity=Severity.BLOCKING,
                    detail=f"{shard.location} is named in the manifest and is not here",
                )
            )
            continue

        payload = path.read_bytes()
        if len(payload) != shard.byte_length:
            findings.append(
                ValidationFinding(
                    code="shard_length",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{shard.location} is {len(payload)} bytes, the manifest records "
                        f"{shard.byte_length}"
                    ),
                )
            )
        if sha256_bytes(payload) != shard.sha256:
            findings.append(
                ValidationFinding(
                    code="shard_digest",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{shard.location} digests to {sha256_bytes(payload)[:12]}…, the "
                        f"manifest records {shard.sha256[:12]}…"
                    ),
                )
            )

        lines = [line for line in payload.split(b"\n") if line.strip()]
        if len(lines) != shard.row_count:
            findings.append(
                ValidationFinding(
                    code="shard_row_count",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{shard.location} holds {len(lines)} rows, the manifest records "
                        f"{shard.row_count}"
                    ),
                )
            )
        counts[shard.kind] = len(lines)

        if shard.kind == INVENTORY_KIND:
            rows.setdefault(INVENTORY_KIND, {})  # type: ignore[arg-type]
            _parse_inventory(shard.location, lines, rows, findings)
            continue

        try:
            model = record_for(shard.kind)
        except (KeyError, ValueError):
            findings.append(
                ValidationFinding(
                    code="shard_kind_unknown",
                    severity=Severity.ADVISORY,
                    detail=(
                        f"{shard.location} is of kind {shard.kind!r}, which this version "
                        "does not know; its rows are preserved and not interpreted"
                    ),
                )
            )
            continue

        kind = RecordKind(shard.kind)
        bucket = rows.setdefault(kind, {})
        for number, line in enumerate(lines, start=1):
            try:
                record = model.model_validate(json.loads(line))
            except Exception as exc:
                findings.append(
                    ValidationFinding(
                        code="row_unparseable",
                        severity=Severity.BLOCKING,
                        detail=f"{shard.location} line {number} does not parse ({exc})",
                    )
                )
                continue
            if record.record_id in bucket:
                findings.append(
                    ValidationFinding(
                        code="row_duplicated",
                        severity=Severity.BLOCKING,
                        detail=f"{shard.location}: {record.record_id} appears twice",
                    )
                )
            bucket[record.record_id] = record
    return rows, counts


def _parse_inventory(
    location: str,
    lines: list[bytes],
    rows: dict[Any, dict[UUID, Any]],
    findings: list[ValidationFinding],
) -> None:
    bucket = rows[INVENTORY_KIND]
    for number, line in enumerate(lines, start=1):
        try:
            ref = ArtifactRef.model_validate(json.loads(line))
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    code="row_unparseable",
                    severity=Severity.BLOCKING,
                    detail=f"{location} line {number} does not parse ({exc})",
                )
            )
            continue
        bucket[ref.artifact_id] = ref


# ── the closure ────────────────────────────────────────────────────────────


def _check_closure(rows: dict[Any, dict[UUID, Any]], findings: list[ValidationFinding]) -> None:
    """Every reference resolves inside the snapshot.

    This is what distinguishes an archive from a table dump. A release entry
    naming a work that did not travel is an archive that cannot be served, and
    the producer's database — the only place that could have explained it — is
    exactly what an importer does not have.
    """
    for kind, edges in _EDGES.items():
        for record in rows.get(kind, {}).values():
            for attribute, target, required in edges:
                value = getattr(record, attribute, None)
                if value is None:
                    continue
                if value not in rows.get(target, {}):
                    findings.append(_dangling(kind, record, attribute, target, value, required))

    for kind, edges in _LIST_EDGES.items():
        for record in rows.get(kind, {}).values():
            for attribute, target, required in edges:
                for value in getattr(record, attribute, ()) or ():
                    if value not in rows.get(target, {}):
                        findings.append(_dangling(kind, record, attribute, target, value, required))

    # Artifacts a record names must be in the inventory, or the bytes have no
    # entry saying where they are or whether they are required.
    inventory = rows.get(INVENTORY_KIND, {})
    for kind in (RecordKind.ASSET, RecordKind.RENDITION):
        for record in rows.get(kind, {}).values():
            artifact_id = getattr(record, "artifact_id", None)
            if artifact_id is not None and artifact_id not in inventory:
                findings.append(
                    ValidationFinding(
                        code="artifact_not_in_inventory",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"{kind.value} {record.record_id} names artifact {artifact_id}, "
                            "which the inventory does not list"
                        ),
                    )
                )


def _dangling(
    kind: RecordKind,
    record: Any,
    attribute: str,
    target: RecordKind,
    value: UUID,
    required: bool,
) -> ValidationFinding:
    return ValidationFinding(
        code="reference_unresolved",
        severity=Severity.BLOCKING if required else Severity.ADVISORY,
        detail=(
            f"{kind.value} {record.record_id}.{attribute} names {target.value} {value}, "
            "which did not travel in this snapshot"
        ),
    )


def _check_inventory(
    directory: Path,
    manifest: SnapshotManifest,
    rows: dict[Any, dict[UUID, Any]],
    findings: list[ValidationFinding],
) -> None:
    """The required-artifact digest recomputes.

    It lets an importer check the set it was given against the set the producer
    meant to send, before it has fetched a single payload.
    """
    inventory = rows.get(INVENTORY_KIND, {})
    if manifest.required_object_inventory_digest is None:
        findings.append(
            ValidationFinding(
                code="inventory_digest_absent",
                severity=Severity.ADVISORY,
                detail="the manifest records no required-artifact digest",
            )
        )
        return
    actual = sha256_bytes(
        canonical_json(sorted(ref.sha256 for ref in inventory.values() if ref.required))
    )
    if actual != manifest.required_object_inventory_digest:
        findings.append(
            ValidationFinding(
                code="inventory_digest",
                severity=Severity.BLOCKING,
                detail=(
                    "the required artifacts in the inventory do not digest to what the "
                    "manifest records: the snapshot is not the set the producer sent"
                ),
            )
        )


def _check_signature(
    manifest: SnapshotManifest,
    findings: list[ValidationFinding],
    *,
    verifier: Any | None,
    require: bool,
) -> None:
    """Who approved this, as distinct from whether its bytes survived.

    Every other check in this module establishes integrity. This is the only
    one that establishes authority, and the difference matters because anyone
    who can write to the bucket can produce a snapshot whose every digest is
    correct.
    """
    if verifier is None:
        if manifest.signature:
            findings.append(
                ValidationFinding(
                    code="signature_unchecked",
                    severity=Severity.BLOCKING if require else Severity.ADVISORY,
                    detail=(
                        f"signed by {manifest.key_identifier!r} and this deployment has "
                        "no trusted keys configured, so the signature was not checked"
                    ),
                )
            )
        elif require:
            findings.append(
                ValidationFinding(
                    code="signature_required",
                    severity=Severity.BLOCKING,
                    detail="this deployment requires a signature and the snapshot has none",
                )
            )
        return

    statement = statement_for(manifest)
    verdict = evaluate_signature(
        signature=manifest.signature,
        key_identifier=manifest.key_identifier,
        trusted_key_ids=verifier.trusted_key_ids,
        verify=lambda: verifier.verify(
            statement.to_bytes(),
            manifest.signature or "",
            key_identifier=manifest.key_identifier or "",
        ),
    )
    if verdict.verified:
        return
    findings.append(
        ValidationFinding(
            code=f"signature_{verdict.status.value}",
            severity=(
                Severity.BLOCKING
                if verdict.blocks(require_signature=require)
                else Severity.ADVISORY
            ),
            detail=verdict.detail,
        )
    )


def _check_projections(
    directory: Path,
    manifest: SnapshotManifest,
    rows: dict[Any, dict[UUID, Any]],
    findings: list[ValidationFinding],
) -> dict[str, int]:
    """Chunks and vectors, decoded and checked rather than taken on trust.

    A vector that arrives wrong does not raise anywhere downstream. It makes a
    passage unreachable, or ranks it first, depending on the backend — so the
    decode, the dimension and the finiteness are checked here, where there is
    still something to say about it.
    """
    counts: dict[str, int] = {}
    releases = rows.get(RecordKind.RELEASE, {})

    for ref in manifest.projection_refs:
        if ref.release_id not in releases:
            findings.append(
                ValidationFinding(
                    code="projection_without_release",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"a projection names release {ref.release_id}, which is not in "
                        "this snapshot"
                    ),
                )
            )
            continue

        chunk_keys: set[str] = set()
        for shard in ref.shards:
            path = directory / shard.location
            if not path.is_file():
                findings.append(
                    ValidationFinding(
                        code="shard_absent",
                        severity=Severity.BLOCKING,
                        detail=f"{shard.location} is named in the manifest and is not here",
                    )
                )
                continue
            payload = path.read_bytes()
            if sha256_bytes(payload) != shard.sha256:
                findings.append(
                    ValidationFinding(
                        code="shard_digest",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"{shard.location} digests to {sha256_bytes(payload)[:12]}…, "
                            f"the manifest records {shard.sha256[:12]}…"
                        ),
                    )
                )
            lines = [line for line in payload.split(b"\n") if line.strip()]
            if len(lines) != shard.row_count:
                findings.append(
                    ValidationFinding(
                        code="shard_row_count",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"{shard.location} holds {len(lines)} rows, the manifest "
                            f"records {shard.row_count}"
                        ),
                    )
                )
            counts[shard.kind] = counts.get(shard.kind, 0) + len(lines)

            if shard.kind == PROJECTION_CHUNK_KIND:
                chunk_keys |= _check_chunk_shard(shard.location, lines, findings)
            elif shard.kind == PROJECTION_VECTOR_KIND:
                _check_vector_shard(shard.location, lines, ref, chunk_keys, findings)
    return counts


def _check_chunk_shard(
    location: str, lines: list[bytes], findings: list[ValidationFinding]
) -> set[str]:
    keys: set[str] = set()
    for number, line in enumerate(lines, start=1):
        try:
            record = ProjectionChunkRecord.model_validate(json.loads(line))
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    code="row_unparseable",
                    severity=Severity.BLOCKING,
                    detail=f"{location} line {number} does not parse ({exc})",
                )
            )
            continue
        keys.add(record.chunk_key)
        if sha256_bytes(record.content.encode("utf-8")) != record.content_sha256:
            findings.append(
                ValidationFinding(
                    code="chunk_content_digest",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{location}: chunk {record.chunk_key} does not match its own "
                        "content digest, so the passage changed in transit"
                    ),
                )
            )
    return keys


def _check_vector_shard(
    location: str,
    lines: list[bytes],
    ref: Any,
    chunk_keys: set[str],
    findings: list[ValidationFinding],
) -> None:
    for number, line in enumerate(lines, start=1):
        try:
            record = ProjectionVectorRecord.model_validate(json.loads(line))
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    code="row_unparseable",
                    severity=Severity.BLOCKING,
                    detail=f"{location} line {number} does not parse ({exc})",
                )
            )
            continue

        if record.encoding != VECTOR_ENCODING:
            findings.append(
                ValidationFinding(
                    code="vector_encoding_unknown",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{location} line {number} declares encoding "
                        f"{record.encoding!r}, which this version cannot decode"
                    ),
                )
            )
            continue

        try:
            values = record.values()
        except ValueError as exc:
            findings.append(
                ValidationFinding(
                    code="vector_undecodable",
                    severity=Severity.BLOCKING,
                    detail=f"{location} line {number}: {exc}",
                )
            )
            continue

        for problem in vector_problems(
            values,
            dimensions=ref.embedding_dimensions or record.dimensions,
            # The normalisation the profile declares is not in the snapshot, so
            # l2 is assumed: it is what every profile in this application uses,
            # and a vector that is normalised when nothing required it costs
            # nothing. A profile that genuinely is not normalised would need
            # this carried, and that belongs with signed policy metadata.
            normalisation="l2",
        ):
            findings.append(
                ValidationFinding(
                    code="vector_invalid",
                    severity=Severity.BLOCKING,
                    detail=f"{location} line {number}: {problem}",
                )
            )

        if chunk_keys and record.chunk_key not in chunk_keys:
            findings.append(
                ValidationFinding(
                    code="vector_without_chunk",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{location}: vector for chunk {record.chunk_key}, which has no "
                        "chunk row in this projection — it would be unreachable"
                    ),
                )
            )


def _check_releases(
    manifest: SnapshotManifest,
    rows: dict[Any, dict[UUID, Any]],
    findings: list[ValidationFinding],
) -> None:
    """Every release the manifest claims is in the catalog, and servable."""
    releases = rows.get(RecordKind.RELEASE, {})
    for release_id in manifest.scientific_release_ids:
        if release_id not in releases:
            findings.append(
                ValidationFinding(
                    code="release_absent",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"the manifest names release {release_id} and the catalog has no row for it"
                    ),
                )
            )
    for alias, release_id in manifest.selected_alias_targets.items():
        if release_id not in releases:
            findings.append(
                ValidationFinding(
                    code="alias_dangling",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"alias {alias!r} points at release {release_id}, which is not in "
                        "this snapshot: a reader would resolve it to nothing"
                    ),
                )
            )
    for release in releases.values():
        if not release.scope_version_ids:
            findings.append(
                ValidationFinding(
                    code="release_without_scope",
                    severity=Severity.ADVISORY,
                    detail=(
                        f"release {release.record_id} names no scope version, so a reader "
                        "can show what was found and not what was looked for"
                    ),
                )
            )
