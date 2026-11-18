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
5. **The retention edges close too.** Every payload in the inventory names
   what it was derived from and which rights decisions govern it, and both
   resolve. An inventory that understated what governs a payload is how a
   revocation misses bytes, or how a purge removes bytes another acquisition
   still needs.
6. **Under a policy checkpoint, if one is given.** The checkpoint is checked on
   its own terms — signature, sequence, freshness — and then applied, and the
   report says what it withholds from this snapshot and why. A snapshot is not
   *invalid* because current policy withholds some of it; it is served
   narrower, and an operator should know by how much.

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
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
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
from lab_assistant.contracts.archive import (
    ArtifactRef,
    SnapshotManifest,
    content_location,
    is_safe_location,
)
from lab_assistant.contracts.archive_catalog import RecordKind, record_for
from lab_assistant.contracts.archive_policy import POLICY_FORMAT_NAME, PolicyCheckpoint
from lab_assistant.contracts.archive_projection import (
    VECTOR_ENCODING,
    ProjectionChunkRecord,
    ProjectionVectorRecord,
    vector_problems,
)
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.release import ReleaseItemRecord
from lab_assistant.contracts.signing import policy_statement_for, statement_for
from lab_assistant.domain.archive_policy import (
    CheckpointStatus,
    CheckpointVerdict,
    PolicyOverlay,
    evaluate_checkpoint,
    overlay,
)
from lab_assistant.domain.signing import evaluate_signature

__all__ = [
    "PolicyValidationReport",
    "Severity",
    "ValidationFinding",
    "ValidationReport",
    "validate_policy_checkpoint",
    "validate_policy_payload",
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
    #: What the policy checkpoint withholds from this snapshot, when one was
    #: given and could be relied on.
    policy: PolicyOverlay | None = None

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
            "policy_sequence": self.manifest.policy_sequence if self.manifest else None,
            "counts": dict(sorted(self.counts.items())),
            "findings": [finding.as_dict() for finding in self.findings],
            "policy": self.policy.as_dict() if self.policy else None,
            "blocking": len(self.blocking),
            "valid": self.ok,
        }


@dataclass(frozen=True, slots=True)
class PolicyValidationReport:
    """One checkpoint, checked on its own terms."""

    location: str
    checkpoint: PolicyCheckpoint | None = None
    verdict: CheckpointVerdict | None = None
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocks)

    @property
    def ok(self) -> bool:
        return self.checkpoint is not None and not self.blocking

    def as_dict(self) -> dict[str, Any]:
        checkpoint = self.checkpoint
        return {
            "location": self.location,
            "library_id": str(checkpoint.library_id) if checkpoint else None,
            "policy_sequence": checkpoint.policy_sequence if checkpoint else None,
            "digest": checkpoint.signing_digest() if checkpoint else None,
            "issued_at": checkpoint.issued_at.isoformat() if checkpoint else None,
            "valid_until": checkpoint.valid_until.isoformat() if checkpoint else None,
            "public_valid_until": (
                checkpoint.public_valid_until.isoformat() if checkpoint else None
            ),
            "counts": {
                "rights": len(checkpoint.rights),
                "works": len(checkpoint.works),
                "releases": len(checkpoint.releases),
                "tombstones": len(checkpoint.tombstones),
            }
            if checkpoint
            else {},
            "verdict": self.verdict.as_dict() if self.verdict else None,
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
    RecordKind.PARSE_RUN: (("asset_id", RecordKind.ASSET, True),),
    RecordKind.RENDITION: (
        ("asset_id", RecordKind.ASSET, True),
        # Required: text of unknown origin is not evidence, and a reader that
        # enforces its keys cannot store a rendition without its parse.
        ("parse_run_id", RecordKind.PARSE_RUN, True),
        ("work_id", RecordKind.WORK, False),
        ("supersedes_rendition_id", RecordKind.RENDITION, False),
    ),
    RecordKind.SPAN: (("rendition_id", RecordKind.RENDITION, True),),
    RecordKind.EXPERIMENT: (
        ("work_id", RecordKind.WORK, True),
        ("rendition_id", RecordKind.RENDITION, False),
    ),
    RecordKind.CLAIM: (
        ("work_id", RecordKind.WORK, True),
        ("experiment_id", RecordKind.EXPERIMENT, True),
        ("subject_entity_id", RecordKind.ENTITY, True),
        ("unit_entity_id", RecordKind.ENTITY, True),
    ),
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
    # A snapshot of one release still names the release it replaced.
    RecordKind.RELEASE: (("parent_release_id", RecordKind.RELEASE, False),),
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
    policy: PolicyCheckpoint | None = None,
    now: datetime | None = None,
    payloads: bool = False,
) -> ValidationReport:
    """Validate a snapshot directory. Touches nothing but the directory.

    ``payloads`` re-digests every payload the inventory says travelled, and
    checks that every one it says was withheld did not. Off by default because
    it reads every byte; a fetch, and `lab archive verify --payloads`, turn it
    on.

    ``verifier`` carries the keys this deployment was configured to trust.
    Without one the signature is reported and not checked, which is honest:
    "nobody told me whose key to expect" is not "this is fine".

    ``policy`` is a checkpoint to apply, and ``now`` is when: freshness is a
    statement about a moment, so the caller names it rather than this module
    reading a clock.
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
    _check_retention_edges(rows, findings)
    _check_entry_digests(rows, findings)
    _check_releases(manifest, rows, findings)
    if payloads:
        counts |= _check_payloads(directory, rows, findings)
    chunk_renditions: dict[str, tuple[UUID, UUID]] = {}
    counts |= _check_projections(directory, manifest, rows, findings, chunk_renditions)
    _check_signature(manifest, findings, verifier=verifier, require=require_signature)

    applied = None
    if policy is not None:
        applied = _apply_policy(
            manifest,
            rows,
            policy,
            findings,
            chunk_renditions=chunk_renditions,
            verifier=verifier,
            require=require_signature,
            now=now if now is not None else policy.issued_at,
        )
        if applied is not None:
            counts |= applied.counts()
            counts["policy_withheld_chunk"] = sum(
                1
                for release_id, rendition_id in chunk_renditions.values()
                if rendition_id in applied.withheld_renditions
                or release_id in applied.withdrawn_releases
            )
    elif manifest.policy_sequence == 0:
        findings.append(
            ValidationFinding(
                code="policy_not_issued",
                severity=Severity.ADVISORY,
                detail=(
                    "this snapshot was built before the library issued any policy "
                    "checkpoint; a reader applies its own rights records until one exists"
                ),
            )
        )

    return ValidationReport(
        directory=str(directory),
        manifest=manifest,
        counts=counts,
        findings=tuple(findings),
        policy=applied,
    )


def validate_policy_checkpoint(
    path: Path,
    *,
    verifier: Any | None = None,
    require_signature: bool = False,
    now: datetime | None = None,
    library_id: UUID | None = None,
    last_seen_sequence: int | None = None,
    last_seen_digest: str | None = None,
) -> PolicyValidationReport:
    """Check one checkpoint file. Touches nothing but the file.

    What a reader asks before it applies a checkpoint: does it parse, is it the
    object its name says, did a key this deployment trusts sign it, and does it
    move policy forward rather than back. ``now`` defaults to the moment it
    was issued, which answers "was this ever valid" rather than "is it still".
    """
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return PolicyValidationReport(
            location=str(path),
            findings=(
                ValidationFinding(
                    code="policy_absent",
                    severity=Severity.BLOCKING,
                    detail=f"{path.name} cannot be read ({exc})",
                ),
            ),
        )
    return validate_policy_payload(
        payload,
        name=path.name,
        location=str(path),
        verifier=verifier,
        require_signature=require_signature,
        now=now,
        library_id=library_id,
        last_seen_sequence=last_seen_sequence,
        last_seen_digest=last_seen_digest,
    )


def validate_policy_payload(
    payload: bytes,
    *,
    name: str,
    location: str | None = None,
    verifier: Any | None = None,
    require_signature: bool = False,
    now: datetime | None = None,
    library_id: UUID | None = None,
    last_seen_sequence: int | None = None,
    last_seen_digest: str | None = None,
) -> PolicyValidationReport:
    """The same checks over bytes, for a checkpoint fetched from the archive.

    Over the bytes as fetched rather than a re-serialisation of a parsed copy:
    the bytes are what was signed, and what a reader actually holds.
    """
    findings: list[ValidationFinding] = []
    try:
        checkpoint = PolicyCheckpoint.model_validate(json.loads(payload))
    except Exception as exc:
        return PolicyValidationReport(
            location=location or name,
            findings=(
                ValidationFinding(
                    code="policy_unparseable",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{name} does not parse as a policy checkpoint this version can read "
                        f"({exc}). A field this version does not know is refused rather "
                        "than ignored: in a stream that withholds, it is most likely a denial"
                    ),
                ),
            ),
        )

    verdict = _check_checkpoint(
        checkpoint,
        findings,
        name=name,
        verifier=verifier,
        require=require_signature,
        library_id=library_id or checkpoint.library_id,
        now=now if now is not None else checkpoint.issued_at,
        last_seen_sequence=last_seen_sequence,
        last_seen_digest=last_seen_digest,
    )
    return PolicyValidationReport(
        location=location or name,
        checkpoint=checkpoint,
        verdict=verdict,
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
        if not is_safe_location(shard.location):
            findings.append(_unsafe(shard.location))
            continue
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
    withheld = {ref.artifact_id for ref in inventory.values() if not ref.is_resolvable}
    if withheld != set(manifest.missing_optional_artifacts):
        findings.append(
            ValidationFinding(
                code="inventory_missing_disagrees",
                severity=Severity.BLOCKING,
                detail=(
                    f"the inventory withholds {len(withheld)} artifact(s) and the manifest lists "
                    f"{len(manifest.missing_optional_artifacts)} as missing: an importer cannot "
                    "tell which account is true"
                ),
            )
        )
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


def _check_retention_edges(
    rows: dict[Any, dict[UUID, Any]], findings: list[ValidationFinding]
) -> None:
    """Every payload's retention edges resolve, and say what the records say.

    These are what turn "decision D was revoked" into "these bytes are
    withheld" without the producer's database: a reader evicting its cache, and
    eventually a purge, work from the inventory. So an edge that names nothing
    is blocking, and so is one that is missing where a record says it should
    exist — an inventory that understates what governs a payload is how a
    revocation misses bytes, or how a purge removes bytes another acquisition
    still needs.
    """
    inventory: dict[UUID, ArtifactRef] = rows.get(INVENTORY_KIND, {})
    decisions = rows.get(RecordKind.RIGHTS_DECISION, {})

    for ref in inventory.values():
        for dependency in ref.depends_on:
            if dependency not in inventory:
                findings.append(
                    ValidationFinding(
                        code="retention_dependency_unresolved",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"artifact {ref.artifact_id} depends on {dependency}, which the "
                            "inventory does not list: its retention cannot be decided"
                        ),
                    )
                )
        for decision_id in ref.governed_by:
            if decision_id not in decisions:
                findings.append(
                    ValidationFinding(
                        code="retention_decision_unresolved",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"artifact {ref.artifact_id} is governed by rights decision "
                            f"{decision_id}, which did not travel in this snapshot"
                        ),
                    )
                )

    assets = rows.get(RecordKind.ASSET, {})
    for asset in assets.values():
        held = inventory.get(asset.artifact_id) if asset.artifact_id else None
        if held is None or asset.rights_decision_id is None:
            continue
        if asset.rights_decision_id not in held.governed_by:
            findings.append(
                ValidationFinding(
                    code="retention_edge_missing",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"asset {asset.record_id} is governed by rights decision "
                        f"{asset.rights_decision_id} and its artifact does not say so: a "
                        "revocation of that decision would not reach these bytes"
                    ),
                )
            )

    for rendition in rows.get(RecordKind.RENDITION, {}).values():
        held = inventory.get(rendition.artifact_id) if rendition.artifact_id else None
        source = assets.get(rendition.asset_id)
        if held is None or source is None or source.artifact_id is None:
            continue
        if source.artifact_id not in held.depends_on:
            findings.append(
                ValidationFinding(
                    code="retention_edge_missing",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"rendition {rendition.record_id} is the text of asset "
                        f"{rendition.asset_id} and its artifact does not depend on that "
                        "asset's: withholding the asset would leave its text servable"
                    ),
                )
            )


def _unsafe(location: str) -> ValidationFinding:
    return ValidationFinding(
        code="shard_location_unsafe",
        severity=Severity.BLOCKING,
        detail=(
            f"the manifest names a shard at {location[:120]!r}, which is not a plain relative "
            "path inside the snapshot. Every location here is written by this code, so this is "
            "tampering, and nothing is read from it"
        ),
    )


def _check_entry_digests(
    rows: dict[Any, dict[UUID, Any]], findings: list[ValidationFinding]
) -> None:
    """Each release entry is the entry its producer digested.

    The producer's incremental builds compare these digests, and a reader that
    rebuilt an entry from fields that did not all travel would disagree with
    them silently. Rebuilding it here and comparing is how a field that stopped
    travelling is caught before an import depends on it.
    """
    for entry in rows.get(RecordKind.RELEASE_ITEM, {}).values():
        if entry.content_digest is None:
            continue
        rebuilt = ReleaseItemRecord(
            work_id=entry.work_id,
            manifestation_id=entry.manifestation_id,
            asset_id=entry.asset_id,
            rendition_id=entry.rendition_id,
            rendition_digest=entry.rendition_digest,
            claim_revision_ids=tuple(entry.claim_revision_ids),
            access_policy_snapshot_id=entry.access_policy_snapshot_id,
            inclusion_reason=entry.inclusion_reason,
        ).content_digest()
        if rebuilt != entry.content_digest:
            findings.append(
                ValidationFinding(
                    code="entry_digest",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"release entry {entry.record_id} digests to {rebuilt[:12]}… and records "
                        f"{entry.content_digest[:12]}…: something its producer digested did not "
                        "travel"
                    ),
                )
            )


def _check_payloads(
    directory: Path, rows: dict[Any, dict[UUID, Any]], findings: list[ValidationFinding]
) -> dict[str, int]:
    """Every payload that travelled is intact, and none that was withheld did.

    The second half matters as much as the first. A payload the inventory says
    was withheld by rights and that is nonetheless here is content that left
    the producer without the permission the inventory says it lacked.
    """
    inventory: dict[UUID, ArtifactRef] = rows.get(INVENTORY_KIND, {})
    present = 0
    for ref in inventory.values():
        try:
            location = content_location(ref.sha256)
        except ValueError:
            findings.append(
                ValidationFinding(
                    code="payload_digest_malformed",
                    severity=Severity.BLOCKING,
                    detail=f"artifact {ref.artifact_id} names {ref.sha256[:80]!r} as its digest",
                )
            )
            continue
        path = directory / location
        if not ref.is_resolvable:
            if path.exists():
                findings.append(
                    ValidationFinding(
                        code="payload_withheld_present",
                        severity=Severity.BLOCKING,
                        detail=(
                            f"artifact {ref.artifact_id} is recorded as "
                            f"{ref.unavailable.value if ref.unavailable else 'withheld'} and its "
                            "bytes are in the snapshot anyway"
                        ),
                    )
                )
            continue
        if not path.is_file():
            findings.append(
                ValidationFinding(
                    code="payload_missing",
                    severity=Severity.BLOCKING if ref.required else Severity.ADVISORY,
                    detail=(
                        f"artifact {ref.artifact_id} ({ref.sha256[:12]}…) is not in the snapshot"
                    ),
                )
            )
            continue
        data = path.read_bytes()
        if len(data) != ref.byte_length or sha256_bytes(data) != ref.sha256:
            findings.append(
                ValidationFinding(
                    code="payload_corrupt",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"artifact {ref.artifact_id} is {len(data)} bytes digesting to "
                        f"{sha256_bytes(data)[:12]}…; the inventory records {ref.byte_length} "
                        f"bytes and {ref.sha256[:12]}…"
                    ),
                )
            )
            continue
        present += 1
    return {"payload": present}


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

    _verify_statement(
        statement_for(manifest).to_bytes(),
        signature=manifest.signature,
        key_identifier=manifest.key_identifier,
        findings=findings,
        verifier=verifier,
        require=require,
        code_prefix="signature",
    )


def _verify_statement(
    statement: bytes,
    *,
    signature: str | None,
    key_identifier: str | None,
    findings: list[ValidationFinding],
    verifier: Any,
    require: bool,
    code_prefix: str,
) -> None:
    """One trust policy for every signed thing: `domain/signing.py` decides."""
    verdict = evaluate_signature(
        signature=signature,
        key_identifier=key_identifier,
        trusted_key_ids=verifier.trusted_key_ids,
        verify=lambda: verifier.verify(
            statement, signature or "", key_identifier=key_identifier or ""
        ),
    )
    if verdict.verified:
        return
    findings.append(
        ValidationFinding(
            code=f"{code_prefix}_{verdict.status.value}",
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
    chunk_renditions: dict[str, tuple[UUID, UUID]] | None = None,
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
            if not is_safe_location(shard.location):
                findings.append(_unsafe(shard.location))
                continue
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
                chunk_keys |= _check_chunk_shard(
                    shard.location, lines, findings, chunk_renditions=chunk_renditions
                )
            elif shard.kind == PROJECTION_VECTOR_KIND:
                _check_vector_shard(shard.location, lines, ref, chunk_keys, findings)
    return counts


def _check_chunk_shard(
    location: str,
    lines: list[bytes],
    findings: list[ValidationFinding],
    *,
    chunk_renditions: dict[str, tuple[UUID, UUID]] | None = None,
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
        if chunk_renditions is not None:
            # Keyed by release as well: copied chunks share keys across
            # releases by design, and a projection is joined on its full key.
            chunk_renditions[f"{record.release_id}:{record.chunk_key}"] = (
                record.release_id,
                record.rendition_id,
            )
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


# ── policy ─────────────────────────────────────────────────────────────────

#: `<sequence>-<digest>.json`, the name `policy_location` gives a checkpoint.
_CHECKPOINT_NAME = re.compile(r"^(?P<sequence>\d{12})-(?P<digest>[0-9a-f]{64})\.json$")


def _check_checkpoint(
    checkpoint: PolicyCheckpoint,
    findings: list[ValidationFinding],
    *,
    name: str | None,
    verifier: Any | None,
    require: bool,
    library_id: UUID,
    now: datetime,
    last_seen_sequence: int | None = None,
    last_seen_digest: str | None = None,
    minimum_sequence: int = 0,
    minimum_digest: str | None = None,
) -> CheckpointVerdict:
    """A checkpoint on its own terms: what it is, who signed it, what it is worth."""
    if checkpoint.format_name != POLICY_FORMAT_NAME:
        findings.append(
            ValidationFinding(
                code="policy_format_unknown",
                severity=Severity.BLOCKING,
                detail=f"{checkpoint.format_name!r} is not a policy checkpoint format",
            )
        )
    major = checkpoint.format_version.split(".")[0]
    if major != "1":
        findings.append(
            ValidationFinding(
                code="policy_format_unsupported",
                severity=Severity.BLOCKING,
                detail=(f"policy format {checkpoint.format_version} is not a 1.x this code reads"),
            )
        )

    digest = checkpoint.signing_digest()
    matched = _CHECKPOINT_NAME.match(name or "")
    if matched is not None and (
        int(matched["sequence"]) != checkpoint.policy_sequence or matched["digest"] != digest
    ):
        findings.append(
            ValidationFinding(
                code="policy_edited",
                severity=Severity.BLOCKING,
                detail=(
                    f"{name} names sequence {int(matched['sequence'])} and digest "
                    f"{matched['digest'][:12]}…, and holds sequence "
                    f"{checkpoint.policy_sequence} digesting to {digest[:12]}…: it was "
                    "changed after it was written"
                ),
            )
        )

    for label, values in (
        ("rights decision", [item.decision_id for item in checkpoint.rights]),
        ("work", [item.work_id for item in checkpoint.works]),
        ("release", [item.release_id for item in checkpoint.releases]),
        ("tombstone", [item.sha256 for item in checkpoint.tombstones]),
    ):
        repeated = sorted(str(value) for value, seen in Counter(values).items() if seen > 1)
        if repeated:
            findings.append(
                ValidationFinding(
                    code="policy_entry_duplicated",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"{label} {repeated[0]} has two standings, and nothing says which one holds"
                    ),
                )
            )

    if verifier is None:
        if checkpoint.signature:
            findings.append(
                ValidationFinding(
                    code="policy_signature_unchecked",
                    severity=Severity.BLOCKING if require else Severity.ADVISORY,
                    detail=(
                        f"signed by {checkpoint.key_identifier!r} and this deployment has no "
                        "trusted keys configured, so the signature was not checked"
                    ),
                )
            )
        elif require:
            findings.append(
                ValidationFinding(
                    code="policy_signature_required",
                    severity=Severity.BLOCKING,
                    detail="this deployment requires a signature and the checkpoint has none",
                )
            )
    else:
        _verify_statement(
            policy_statement_for(checkpoint).to_bytes(),
            signature=checkpoint.signature,
            key_identifier=checkpoint.key_identifier,
            findings=findings,
            verifier=verifier,
            require=require,
            code_prefix="policy_signature",
        )

    verdict = evaluate_checkpoint(
        checkpoint,
        library_id=library_id,
        now=now,
        last_seen_sequence=last_seen_sequence,
        last_seen_digest=last_seen_digest,
        minimum_sequence=minimum_sequence,
        minimum_digest=minimum_digest,
    )
    if verdict.status is not CheckpointStatus.CURRENT:
        findings.append(
            ValidationFinding(
                code=f"policy_{verdict.status.value}",
                # Stale is a fact about the moment, not about the checkpoint:
                # it was valid, and a newer one is what fixes it. Everything
                # else here means it must not be applied at all.
                severity=Severity.BLOCKING if verdict.refused else Severity.ADVISORY,
                detail=verdict.detail,
            )
        )
    return verdict


def _apply_policy(
    manifest: SnapshotManifest,
    rows: dict[Any, dict[UUID, Any]],
    policy: PolicyCheckpoint,
    findings: list[ValidationFinding],
    *,
    chunk_renditions: dict[str, tuple[UUID, UUID]],
    verifier: Any | None,
    require: bool,
    now: datetime,
) -> PolicyOverlay | None:
    """The checkpoint, checked, then applied to this snapshot's records.

    ``None`` when the checkpoint must not be applied — a refusal is already a
    blocking finding, and an overlay computed from it would be a report about
    policy nobody should rely on.
    """
    before = len([finding for finding in findings if finding.blocks])
    verdict = _check_checkpoint(
        policy,
        findings,
        name=None,
        verifier=verifier,
        require=require,
        library_id=manifest.library_id,
        now=now,
        minimum_sequence=manifest.policy_sequence,
        minimum_digest=manifest.policy_artifact_digest,
    )
    if verdict.refused or len([finding for finding in findings if finding.blocks]) > before:
        return None

    applied = overlay(
        policy,
        verdict=verdict,
        now=now,
        rights=rows.get(RecordKind.RIGHTS_DECISION, {}),
        assets=rows.get(RecordKind.ASSET, {}),
        renditions=rows.get(RecordKind.RENDITION, {}),
        inventory=rows.get(INVENTORY_KIND, {}),
        release_ids=tuple(rows.get(RecordKind.RELEASE, {})),
    )

    counts = applied.counts()
    withheld = (
        counts["policy_withheld_asset"]
        + counts["policy_withheld_rendition"]
        + counts["policy_withdrawn_release"]
    )
    if withheld:
        findings.append(
            ValidationFinding(
                code="policy_withholds",
                severity=Severity.ADVISORY,
                detail=(
                    f"under policy sequence {policy.policy_sequence} this snapshot serves "
                    f"narrower than it was published: {counts['policy_withheld_asset']} "
                    f"asset(s), {counts['policy_withheld_rendition']} rendition(s) and "
                    f"{counts['policy_withdrawn_release']} withdrawn release(s) are withheld"
                ),
            )
        )
    if applied.absent_decisions:
        findings.append(
            ValidationFinding(
                code="policy_decision_absent",
                severity=Severity.ADVISORY,
                detail=(
                    f"{len(applied.absent_decisions)} rights decision(s) in this snapshot are "
                    "not in the checkpoint, so the material they govern is withheld: absent "
                    "is never read as permitted"
                ),
            )
        )
    if applied.retracted_works:
        findings.append(
            ValidationFinding(
                code="policy_retractions",
                severity=Severity.ADVISORY,
                detail=(
                    f"{len(applied.retracted_works)} work(s) have been retracted since "
                    "publication; a reader must show them as retracted"
                ),
            )
        )
    return applied
