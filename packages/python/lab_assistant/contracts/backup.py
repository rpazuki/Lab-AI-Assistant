"""Private recovery backup format, version 1.

This is the **private** half of the archive work, and the distinction matters
more than the similar file names suggest. `contracts/archive.py` describes a
portable scientific library: published releases, meant to be read by another
installation that was never trusted with anything else. This file describes a
recovery backup: accounts, password hashes, chat history, unreviewed evidence,
queue state, audit chains. One is designed to be handed to a reader; the other
must never leave the operator's own encrypted storage.

The question it answers: **what does a person who has lost the VM need in order
to establish that the thing they restored is the thing they backed up?** Not
"what files did we copy" — a directory of bytes with no provenance cannot
answer whether the dump came from a newer schema than the code about to read
it, whether pgvector is present at a compatible version, or which of the two
installations now holding this data is allowed to publish.

Four rules shape it, each one a failure the predecessor's scripts allowed.

**A backup names its origin precisely.** App version, Alembic revision,
PostgreSQL server version, extension versions, object store kind and address.
`restore.sh` assumed all of these and verified none, so an incompatible restore
failed late, during serving, instead of early, during a preflight check.

**Secrets are referenced, never carried.** The manifest lists the *names* of the
environment entries a restore will need and where they come from. An archive
containing the database password turns every off-site copy into a credential
leak, so what travels is a checklist, not a value. The checklist exists because
the alternative — an operator discovering a missing secret after the restore has
begun — is how a four-hour RTO becomes a day.

**Every component is digested, and the manifest digests itself.** A dump
truncated by a full disk is a plausible-looking file. The only thing that
distinguishes it from a good one is a digest recorded when it was written.

**Restoring a backup does not confer the right to publish.** A backup is a
perfect copy of an installation, which means restoring it twice produces two
installations that each believe they are the publisher. The manifest therefore
carries the origin installation's identity and writer epoch as *facts about
where this came from*, and the restore path treats adopting them as a deliberate
fenced handover rather than the default. See `domain/recovery.py`, which holds
the rule.

What this does **not** attempt: taking the dump (that is an adapter, because it
shells out to `pg_dump`), encrypting the result (that is the operator's storage,
and a format that rolled its own encryption would be worse than one that says it
does not), and the publication protocol of phase 2.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "BACKUP_FORMAT_NAME",
    "BACKUP_FORMAT_VERSION",
    "MINIMUM_RESTORE_PROTOCOL",
    "BackupComponent",
    "BackupManifest",
    "ComponentKind",
    "InstallationFingerprint",
    "SecretReference",
    "WorkspaceInventory",
]

BACKUP_FORMAT_NAME = "lab-assistant-recovery"
#: Major.minor. A restore refuses an unknown **major** outright. It may accept an
#: unknown minor, because a backup taken by a newer producer that only added
#: fields is still restorable — but it says so, rather than pretending it
#: understood the whole file.
BACKUP_FORMAT_VERSION = "1.0"
#: The oldest restore implementation that can read this format.
MINIMUM_RESTORE_PROTOCOL = "1.0"


class ComponentKind(StrEnum):
    """What a file in the backup directory is.

    The kinds are separate because their failure modes are separate: a missing
    dump means there is nothing to restore, while a missing object export means
    the database will restore and every citation into it will break. Reporting
    both as "a file is missing" loses the difference between the two.
    """

    #: A logical `pg_dump`, in the custom format, restorable with `pg_restore`.
    #: Logical rather than physical because physical recovery only works inside a
    #: narrow platform and version envelope, and the point of this format is to
    #: survive a move to a VM nobody has provisioned yet.
    DATABASE_DUMP = "database_dump"
    #: Object payloads, written as a filesystem store rooted in the backup. They
    #: come through the same `ObjectStore` port the application uses, so this
    #: backs up an S3 installation exactly as well as a filesystem one — which
    #: the predecessor's `rsync` could not do at all.
    OBJECT_PAYLOADS = "object_payloads"
    #: The migration report describing what was copied and what was deliberately
    #: not: tombstoned payloads, source-only payloads, anything unreadable.
    OBJECT_INVENTORY = "object_inventory"


class SecretReference(BaseModel):
    """A secret the restore will need, named but never carried.

    `where` is prose for a person: "the institution's secret manager", "the TLS
    chain issued for the destination host". It is deliberately not a machine
    address — a backup that pointed at a vault path would be a map to the
    credentials for anyone who stole the backup.
    """

    model_config = {"frozen": True}

    name: str
    required: bool = True
    where: str = "the institution's secret manager"
    note: str | None = None


class BackupComponent(BaseModel):
    """One file or directory in the backup, with the digest that proves it.

    `sha256` over a directory component is the digest of its own inventory
    listing, not of a concatenation of payloads: the objects are individually
    content-addressed already, and re-reading a corpus to produce one number
    would make verification cost a full corpus read every time.
    """

    model_config = {"frozen": True}

    name: str
    kind: ComponentKind
    #: Relative to the backup directory. Never absolute: an absolute path in a
    #: manifest is a path on a machine that, by the time anyone reads this, does
    #: not exist.
    path: str
    sha256: str
    byte_length: int
    item_count: int | None = None
    optional: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class InstallationFingerprint(BaseModel):
    """Everything about an installation that a restore has to match or refuse.

    Collected from the running system rather than from configuration files,
    because what matters is the version that is actually loaded. A compose file
    claiming pgvector 0.8 while the server has 0.5 is exactly the case this is
    here to catch.
    """

    model_config = {"frozen": True}

    #: Identity of the installation, not of the library or the workspace. A
    #: duplicated backup yields a second installation, and that one is new.
    installation_id: UUID
    #: `configured` when an operator set `LAB_INSTALLATION_ID`, `derived` when it
    #: was computed from the installation's own addresses. The difference is
    #: operationally real: a derived identity changes when the installation
    #: moves, so a planned move that means to keep its identity must configure
    #: one before the move rather than discover afterwards that it did not.
    identity_source: str = "derived"
    env: str
    app_version: str
    #: The Alembic revision the database is actually at. `None` means the
    #: database has no `alembic_version` row, which is itself a finding: it is a
    #: schema created by `lab db create` rather than by migrations.
    schema_revision: str | None = None
    database_server_version: str
    #: Extension name to version, e.g. `{"vector": "0.8.2"}`.
    extensions: dict[str, str] = Field(default_factory=dict)
    object_store_kind: str
    #: Bucket and prefix, or filesystem root. An address, never a credential —
    #: and recorded because a restore that silently points at a different store
    #: is the failure the whole storage-migration command exists to prevent.
    object_store_address: str | None = None
    #: Writer epoch at the moment of the backup. The restore path uses it to
    #: refuse a silent second publisher; see `domain/recovery.py`.
    writer_epoch: int = 0

    def as_claims(self) -> dict[str, Any]:
        """The subset a compatibility check compares. Address is excluded: where
        the bytes sit is not part of whether the schema can be read."""
        return {
            "app_version": self.app_version,
            "schema_revision": self.schema_revision,
            "database_server_version": self.database_server_version,
            "extensions": dict(self.extensions),
            "object_store_kind": self.object_store_kind,
        }


class WorkspaceInventory(BaseModel):
    """Counts a restore can check itself against.

    These are the numbers an operator reads out loud during a drill. They are
    not a substitute for `lab db verify-restore` — they cannot tell you a
    manifest digest stopped recomputing — but a restore that came back with
    three of four workspaces is caught here, in seconds, rather than by a
    researcher's broken search a week later.
    """

    model_config = {"frozen": True}

    workspace_id: UUID
    slug: str
    releases: int = 0
    objects: int = 0
    object_bytes: int = 0


class BackupManifest(BaseModel):
    """The recovery manifest: what this backup is, and what it needs to come back."""

    model_config = {"frozen": True}

    format_name: str = BACKUP_FORMAT_NAME
    format_version: str = BACKUP_FORMAT_VERSION
    minimum_restore_protocol: str = MINIMUM_RESTORE_PROTOCOL

    backup_id: UUID
    created_at: datetime
    origin: InstallationFingerprint

    components: tuple[BackupComponent, ...] = ()
    secret_references: tuple[SecretReference, ...] = ()
    workspaces: tuple[WorkspaceInventory, ...] = ()

    #: What is deliberately absent, in the operator's words. An omission nobody
    #: wrote down is indistinguishable from a bug, and "the model cache is not in
    #: here" is a sentence somebody needs to read before they spend an hour
    #: looking for it.
    excluded: tuple[str, ...] = ()
    #: Items that should have been included and could not be. Distinct from
    #: `excluded` because one is a decision and the other is a problem.
    incomplete: tuple[str, ...] = ()

    @property
    def format_major(self) -> int:
        return int(self.format_version.split(".")[0])

    @property
    def complete(self) -> bool:
        """A backup with a known hole is not a backup you can plan around."""
        return not self.incomplete

    def component(self, kind: ComponentKind) -> BackupComponent | None:
        for item in self.components:
            if item.kind is kind:
                return item
        return None

    def manifest_sha256(self) -> str:
        """Digest over everything but the digest itself.

        Written into `MANIFEST.sha256` beside the manifest so that a verify can
        tell a manifest that was edited from one that was not. It is not a
        signature and does not pretend to be: anybody who can rewrite the
        manifest can rewrite the digest file next to it. Authenticating a backup
        is the job of the encrypted storage it lives in, and saying so here is
        better than a ceremony that looks like authentication and is not.
        """
        return sha256_bytes(canonical_json(self.model_dump(mode="python")))
