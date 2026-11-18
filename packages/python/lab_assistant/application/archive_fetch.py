"""Fetching a published snapshot the way a reader must: GET only (phase 3).

A reader's credential reads known keys and does nothing else. It cannot list
the bucket and it cannot write to it, and it is right that it cannot: a reader
that could write could rewrite the library it serves from, and one that needed
to list would need a broader credential than the job requires (scenario S32).
So everything here follows from the head, by name:

    control/head.json  ->  snapshots/<id>/manifest.json  ->  every shard it names
                                                          ->  content/sha256/… for every
                                                              payload the inventory carries
    control/policy-head.json  ->  policy/<sequence>-<digest>.json

**Nothing fetched is trusted until it is checked, and nothing unchecked is
kept.** The manifest must be the one the head committed to — its signing digest
against the head's — or a bucket writer could swap it under a head they cannot
move. Every shard and payload must match the length and digest the manifest and
inventory declare, and a file that does not is discarded rather than written.
Locations and digests come from a manifest that is only partly verified, so they
are checked before they become paths: a "digest" of `../../.ssh/id_ed25519` is
an attack, not a payload (S34).

**A denial is not an absence.** A 403 raises. A reader that read "forbidden" as
"missing" would conclude the evidence was gone, and the next thing that happens
to evidence that is gone is that somebody acquires it again (S27).

**An interrupted fetch resumes.** A file already in the destination that has the
declared digest is not fetched again, so a retry after a dropped connection
costs only what was not yet copied. A destination holding a *different*
snapshot is refused rather than overwritten: two snapshots mixed in one
directory validate as neither.

What this does not do is import. The result is a directory that validates with
its payloads, which the importer reads; keeping the two apart is what lets a
fetch be retried without touching the reader's database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from lab_assistant.application.archive_catalog import INVENTORY_KIND, MANIFEST_NAME
from lab_assistant.application.archive_control import (
    library_prefix,
    read_catalog_head,
    read_policy_head,
)
from lab_assistant.application.archive_validate import ValidationReport, validate_snapshot
from lab_assistant.application.ports import ArchiveControlStore
from lab_assistant.contracts.archive import (
    ArtifactRef,
    SnapshotManifest,
    content_location,
    is_safe_location,
)
from lab_assistant.contracts.archive_policy import POLICY_DIRECTORY, PolicyCheckpoint
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.publication import HeadDescriptor

__all__ = ["FetchReport", "SnapshotFetcher"]


@dataclass(frozen=True, slots=True)
class FetchReport:
    """What a fetch copied, what it could not, and what the copy is worth."""

    library_id: UUID
    directory: str
    head: HeadDescriptor | None = None
    #: Files written by this run, and ones a previous run had already copied.
    fetched: int = 0
    resumed: int = 0
    payloads: int = 0
    #: Payloads the inventory says were withheld, so none were fetched.
    withheld: int = 0
    bytes_fetched: int = 0
    policy_sequence: int | None = None
    problems: tuple[str, ...] = ()
    validation: ValidationReport | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.validation is not None and self.validation.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": str(self.library_id),
            "directory": self.directory,
            "snapshot_id": str(self.head.snapshot_id) if self.head else None,
            "commit_sequence": self.head.commit_sequence if self.head else None,
            "fetched": self.fetched,
            "resumed": self.resumed,
            "payloads": self.payloads,
            "withheld": self.withheld,
            "bytes_fetched": self.bytes_fetched,
            "policy_sequence": self.policy_sequence,
            "problems": list(self.problems),
            "validation": self.validation.as_dict() if self.validation else None,
            "ok": self.ok,
        }


class SnapshotFetcher:
    """Copies the snapshot a library's head names into a local directory."""

    def __init__(
        self,
        control: ArchiveControlStore,
        *,
        library_id: UUID,
        verifier: Any | None = None,
        require_signature: bool = False,
        max_object_bytes: int | None = None,
    ) -> None:
        self._control = control
        self._library_id = library_id
        self._prefix = library_prefix(library_id)
        self._verifier = verifier
        self._require_signature = require_signature
        #: Refused on the declared size, before anything is allocated to hold it.
        self._max_object_bytes = max_object_bytes or None

    async def fetch(self, destination: Path, *, now: datetime) -> FetchReport:
        destination = Path(destination)
        head, _version = await read_catalog_head(self._control, self._library_id)
        if head is None:
            return FetchReport(
                library_id=self._library_id,
                directory=str(destination),
                problems=("this library has published nothing: its head is absent",),
            )

        problems: list[str] = []
        manifest = await self._manifest(head, destination, problems)
        if manifest is None:
            return FetchReport(
                library_id=self._library_id,
                directory=str(destination),
                head=head,
                problems=tuple(problems),
            )

        tally = _Tally()
        for shard in (
            *manifest.catalog_shards,
            *(shard for ref in manifest.projection_refs for shard in ref.shards),
        ):
            if not is_safe_location(shard.location):
                problems.append(
                    f"the manifest names a shard at {shard.location[:120]!r}, which is not a "
                    "plain relative path; nothing was fetched for it"
                )
                continue
            await self._copy(
                f"{self._prefix}snapshots/{head.snapshot_id}/{shard.location}",
                destination / shard.location,
                sha256=shard.sha256,
                byte_length=shard.byte_length,
                tally=tally,
                problems=problems,
            )

        for ref in _inventory(destination, manifest):
            if not ref.is_resolvable:
                tally.withheld += 1
                continue
            try:
                location = content_location(ref.sha256)
            except ValueError:
                problems.append(f"artifact {ref.artifact_id} names a malformed digest")
                continue
            if await self._copy(
                f"{self._prefix}{location}",
                destination / location,
                sha256=ref.sha256,
                byte_length=ref.byte_length,
                tally=tally,
                problems=problems,
            ):
                tally.payloads += 1

        policy = await self._policy(destination, problems)
        validation = validate_snapshot(
            destination,
            verifier=self._verifier,
            require_signature=self._require_signature,
            policy=policy,
            now=now,
            payloads=True,
        )
        return FetchReport(
            library_id=self._library_id,
            directory=str(destination),
            head=head,
            fetched=tally.fetched,
            resumed=tally.resumed,
            payloads=tally.payloads,
            withheld=tally.withheld,
            bytes_fetched=tally.bytes,
            policy_sequence=policy.policy_sequence if policy else None,
            problems=tuple(problems),
            validation=validation,
        )

    # ── the manifest ───────────────────────────────────────────────────────

    async def _manifest(
        self, head: HeadDescriptor, destination: Path, problems: list[str]
    ) -> SnapshotManifest | None:
        """The manifest the head committed to, or nothing.

        Compared on the signing digest the head records, which is what makes
        the head's commitment mean something: a bucket writer who cannot move
        the head can still replace the object it points at, and this is where
        that is caught.
        """
        key = f"{self._prefix}snapshots/{head.snapshot_id}/{MANIFEST_NAME}"
        data = await self._read(key)
        if data is None:
            problems.append(
                f"the head names snapshot {head.snapshot_id} and its manifest is absent"
            )
            return None
        try:
            manifest = SnapshotManifest.model_validate(json.loads(data))
        except Exception as exc:
            problems.append(f"the manifest does not parse ({exc})")
            return None
        if manifest.signing_digest() != head.snapshot_manifest_digest:
            problems.append(
                "the manifest at the head's location is not the one the head committed to: "
                "it was replaced after publication"
            )
            return None
        if manifest.snapshot_id != head.snapshot_id or manifest.library_id != self._library_id:
            problems.append("the manifest names another snapshot or another library")
            return None

        existing = destination / MANIFEST_NAME
        if existing.is_file() and existing.read_bytes() != data:
            problems.append(
                f"{destination} already holds a different snapshot; fetch into a new "
                "directory rather than mixing two"
            )
            return None
        destination.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(data)
        (destination / "MANIFEST.sha256").write_text(
            f"{sha256_bytes(data)}  {MANIFEST_NAME}\n", encoding="utf-8"
        )
        return manifest

    # ── copying ────────────────────────────────────────────────────────────

    async def _copy(
        self,
        key: str,
        target: Path,
        *,
        sha256: str,
        byte_length: int,
        tally: _Tally,
        problems: list[str],
    ) -> bool:
        """One object, verified before it is kept. ``True`` when it is in place."""
        if target.is_file() and _matches(target.read_bytes(), sha256, byte_length):
            tally.resumed += 1
            return True
        if self._max_object_bytes is not None and byte_length > self._max_object_bytes:
            problems.append(
                f"{key} is declared at {byte_length} bytes, over this reader's limit of "
                f"{self._max_object_bytes}; it was not fetched"
            )
            return False
        data = await self._read(key)
        if data is None:
            problems.append(f"{key} is named by the snapshot and absent from the archive")
            return False
        if not _matches(data, sha256, byte_length):
            # Not written. Bad bytes kept "for diagnosis" are bad bytes the next
            # resume would find and trust.
            problems.append(
                f"{key} is {len(data)} bytes digesting to {sha256_bytes(data)[:12]}…; the "
                f"snapshot declares {byte_length} bytes and {sha256[:12]}…"
            )
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.partial")
        partial.write_bytes(data)
        partial.replace(target)
        tally.fetched += 1
        tally.bytes += len(data)
        return True

    async def _read(self, key: str) -> bytes | None:
        """GET by name. ``None`` only when absent; a denial or outage raises."""
        payload = await self._control.read(key)
        return payload.data if payload is not None else None

    # ── policy ─────────────────────────────────────────────────────────────

    async def _policy(self, destination: Path, problems: list[str]) -> PolicyCheckpoint | None:
        """The current checkpoint, beside the snapshot, so it validates offline."""
        head, _version = await read_policy_head(self._control, self._library_id)
        if head is None:
            return None
        data = await self._read(f"{self._prefix}{head.location}")
        if data is None:
            problems.append(
                f"the policy head names checkpoint {head.policy_sequence}, which is absent"
            )
            return None
        try:
            checkpoint = PolicyCheckpoint.model_validate(json.loads(data))
        except Exception as exc:
            problems.append(f"the current policy checkpoint does not parse ({exc})")
            return None
        if checkpoint.signing_digest() != head.checkpoint_digest:
            problems.append("the policy checkpoint is not the one its head committed to")
            return None
        target = destination / POLICY_DIRECTORY / Path(head.location).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return checkpoint


class _Tally:
    __slots__ = ("bytes", "fetched", "payloads", "resumed", "withheld")

    def __init__(self) -> None:
        self.fetched = 0
        self.resumed = 0
        self.payloads = 0
        self.withheld = 0
        self.bytes = 0


def _matches(data: bytes, sha256: str, byte_length: int) -> bool:
    return len(data) == byte_length and sha256_bytes(data) == sha256


def _inventory(directory: Path, manifest: SnapshotManifest) -> list[ArtifactRef]:
    """The fetched inventory. Unparseable rows are the validator's to report."""
    for shard in manifest.catalog_shards:
        if shard.kind != INVENTORY_KIND or not is_safe_location(shard.location):
            continue
        path = directory / shard.location
        if not path.is_file():
            return []
        refs: list[ArtifactRef] = []
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                refs.append(ArtifactRef.model_validate(json.loads(line)))
            except Exception:  # reported by validation, which re-reads the shard
                continue
        return refs
    return []
