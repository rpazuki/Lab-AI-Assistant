"""Release manifest and export package contracts.

The manifest is canonicalised and hashed, so two builds of the same release are
byte-identical or the difference is a bug. The export package is a
RO-Crate/BagIt-*inspired* profile: documented, checksum-validated, and honest
about not claiming strict conformance until certified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "EXPORT_PACKAGE_PROFILE",
    "ExportPackageManifest",
    "ReleaseDiff",
    "ReleaseItemRecord",
    "ReleaseManifest",
    "ReleaseValidationReport",
]

#: The declared package profile. Bump when the layout changes.
EXPORT_PACKAGE_PROFILE = "lab-assistant-export/1.0 (RO-Crate and BagIt inspired)"


class ReleaseItemRecord(BaseModel):
    """One manifest line. Sorted before hashing."""

    model_config = {"frozen": True}

    work_id: UUID
    manifestation_id: UUID | None = None
    asset_id: UUID | None = None
    rendition_id: UUID | None = None
    rendition_digest: str | None = None
    claim_revision_ids: tuple[UUID, ...] = ()
    access_policy_snapshot_id: UUID | None = None
    inclusion_reason: str = "steward_selected"
    source_discovery_run_ids: tuple[UUID, ...] = ()

    @property
    def sort_key(self) -> tuple[str, str]:
        return (str(self.work_id), str(self.rendition_id or ""))

    def content_digest(self) -> str:
        """Digest over the item's material content, ignoring bookkeeping.

        Two releases sharing this digest for a work can share projection rows,
        which is what makes incremental release builds possible.
        """
        return sha256_bytes(
            canonical_json(
                {
                    "work_id": self.work_id,
                    "manifestation_id": self.manifestation_id,
                    "asset_id": self.asset_id,
                    "rendition_id": self.rendition_id,
                    "rendition_digest": self.rendition_digest,
                    "claim_revision_ids": sorted(str(i) for i in self.claim_revision_ids),
                    "access_policy_snapshot_id": self.access_policy_snapshot_id,
                }
            )
        )


class ReleaseManifest(BaseModel):
    model_config = {"frozen": True}

    release_id: UUID
    workspace_id: UUID
    name: str
    semantic_version: str
    parent_release_id: UUID | None = None
    scope_version_ids: tuple[UUID, ...] = ()
    policy_version_id: UUID | None = None
    embedding_profile_ids: tuple[str, ...] = ()
    chunker_id: str = "structural-v1"
    chunker_version: str = "1"
    items: tuple[ReleaseItemRecord, ...] = ()
    created_at: datetime | None = None

    def canonical_bytes(self) -> bytes:
        return canonical_json(
            {
                "release_id": self.release_id,
                "workspace_id": self.workspace_id,
                "name": self.name,
                "semantic_version": self.semantic_version,
                "parent_release_id": self.parent_release_id,
                "scope_version_ids": sorted(str(i) for i in self.scope_version_ids),
                "policy_version_id": self.policy_version_id,
                "embedding_profile_ids": sorted(self.embedding_profile_ids),
                "chunker": {"id": self.chunker_id, "version": self.chunker_version},
                "items": [
                    item.model_dump(mode="python")
                    for item in sorted(self.items, key=lambda i: i.sort_key)
                ],
            }
        )

    @property
    def manifest_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())

    def item_digests(self) -> dict[UUID, str]:
        return {item.work_id: item.content_digest() for item in self.items}


class ReleaseDiff(BaseModel):
    """What changed against the parent release (lifecycle §12)."""

    model_config = {"frozen": True}

    parent_release_id: UUID | None
    added_work_ids: tuple[UUID, ...] = ()
    removed_work_ids: tuple[UUID, ...] = ()
    changed_work_ids: tuple[UUID, ...] = ()
    unchanged_work_ids: tuple[UUID, ...] = ()
    retracted_work_ids: tuple[UUID, ...] = ()
    corrected_work_ids: tuple[UUID, ...] = ()
    policy_changed: bool = False
    parser_or_template_changed: bool = False

    @property
    def changed_item_count(self) -> int:
        return len(self.added_work_ids) + len(self.changed_work_ids) + len(self.removed_work_ids)

    @property
    def is_full_build(self) -> bool:
        return self.parent_release_id is None


class ReleaseValidationReport(BaseModel):
    """Result of release validation, one row per gate (lifecycle §12/§14)."""

    model_config = {"frozen": True}

    release_id: UUID
    gates: dict[str, bool] = Field(default_factory=dict)
    failures: tuple[str, ...] = ()
    metrics: dict[str, Any] = Field(default_factory=dict)
    manifest_sha256: str | None = None
    validated_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(self.gates.values()) and not self.failures


class ExportPackageManifest(BaseModel):
    """`manifest-sha256.txt` plus the metadata that makes a package self-describing."""

    model_config = {"frozen": True}

    profile: str = EXPORT_PACKAGE_PROFILE
    release_id: UUID
    workspace_id: UUID
    created_at: datetime | None = None
    exported_by: UUID | None = None
    purpose: str = ""
    #: path -> sha256 of every payload file.
    payload_digests: dict[str, str] = Field(default_factory=dict)
    #: path -> sha256 of every tag (metadata) file.
    tag_digests: dict[str, str] = Field(default_factory=dict)
    policy_version_id: UUID | None = None
    rights_summary: tuple[dict[str, Any], ...] = ()
    assets_included: bool = False
    row_counts: dict[str, int] = Field(default_factory=dict)

    def bagit_manifest_text(self) -> str:
        lines = [f"{digest}  {path}" for path, digest in sorted(self.payload_digests.items())]
        return "\n".join(lines) + ("\n" if lines else "")

    def tagmanifest_text(self) -> str:
        lines = [f"{digest}  {path}" for path, digest in sorted(self.tag_digests.items())]
        return "\n".join(lines) + ("\n" if lines else "")
