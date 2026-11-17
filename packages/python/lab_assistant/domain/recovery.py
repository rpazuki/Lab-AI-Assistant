"""Whether a backup may be restored into a given installation, and as what.

The predecessor's `restore.sh` copied files into volumes it named by hand and
started the database. Every compatibility question it needed to ask, it assumed:
that the PostgreSQL major version accepts the dump, that pgvector is installed
at a version the indexes understand, that the application code is not older than
the schema in the dump, and that the person restoring intends to replace the
original installation rather than stand a second one beside it.

Each of those assumptions fails differently, and only one of them fails loudly.

- **A newer dump into older code** restores cleanly and then serves wrong
  answers, because the code does not know about the columns the dump filled.
- **A missing or older extension** fails at the first vector index, which is
  after the restore, after the migrations, and usually after somebody has
  already told the lab it is back.
- **An older PostgreSQL** refuses the dump, which is the one honest failure.
- **A second installation from the same backup** does not fail at all. It works
  perfectly, twice, and the two diverge until somebody notices that a release
  published on one is missing from the other.

So the rules live here, in the domain, with the refusal reasons attached — the
same shape as `identity.py`, and for the same reason: a decision a person will
later have to argue with needs to state its grounds, not just its verdict.

The split between *blocking* and *advisory* is deliberate and is not a severity
scale. Blocking means this restore produces a wrong installation. Advisory means
it produces a correct installation with something an operator must still do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from lab_assistant.contracts.backup import (
    BackupManifest,
    ComponentKind,
    InstallationFingerprint,
)

__all__ = [
    "RestoreFinding",
    "RestoreIntent",
    "RestoreVerdict",
    "evaluate_restore",
]


class RestoreIntent(StrEnum):
    """What the operator means by this restore.

    This is not a detail of presentation. The same bytes restored under the two
    intents produce installations with different rights, and the difference
    cannot be inferred from the backup: only the person doing it knows whether
    the original is gone.
    """

    #: The original installation is gone or being replaced. The restored
    #: installation inherits its identity and its right to publish. The operator
    #: is asserting that the original is fenced — switched off, or demoted — and
    #: that assertion is recorded.
    REPLACE = "replace"
    #: A second installation from the same backup: a drill, a scratch VM, a
    #: staging copy. It gets a new installation id and no publishing right. This
    #: is the default, because the cost of being wrong in this direction is a
    #: restore that cannot publish until somebody promotes it, and the cost of
    #: being wrong in the other direction is two publishers.
    CLONE = "clone"


class Severity(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class RestoreFinding:
    """One reason a restore is refused, or one thing it leaves for a person."""

    code: str
    severity: Severity
    detail: str
    #: What to do about it, where there is a next move worth naming.
    remedy: str | None = None

    @property
    def blocks(self) -> bool:
        return self.severity is Severity.BLOCKING

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class RestoreVerdict:
    """The decision, with every finding that produced it."""

    intent: RestoreIntent
    findings: tuple[RestoreFinding, ...] = ()
    #: The identity the restored installation will run under. Under `CLONE` this
    #: is a fresh id and the origin's is kept only as provenance.
    installation_id: UUID | None = None
    #: Whether the restored installation may publish. A clone may not, until a
    #: deliberate promotion under the handover protocol.
    may_publish: bool = False
    #: True when the schema in the dump is behind the application, so migrations
    #: must run after the restore. That is the normal, supported direction.
    migrations_required: bool = False

    @property
    def blocking(self) -> tuple[RestoreFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocks)

    @property
    def advisory(self) -> tuple[RestoreFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.blocks)

    @property
    def permitted(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "permitted": self.permitted,
            "installation_id": str(self.installation_id) if self.installation_id else None,
            "may_publish": self.may_publish,
            "migrations_required": self.migrations_required,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def evaluate_restore(
    manifest: BackupManifest,
    *,
    target: InstallationFingerprint,
    intent: RestoreIntent = RestoreIntent.CLONE,
    new_installation_id: UUID | None = None,
    origin_is_fenced: bool = False,
    supported_format_major: int = 1,
    migration_order: tuple[str, ...] = (),
    target_relation_count: int = 0,
    clean_requested: bool = False,
) -> RestoreVerdict:
    """Decide whether this backup may be restored into this installation.

    ``migration_order`` is the application's Alembic revisions, oldest first. It
    is passed in rather than read here because the domain may not touch the
    filesystem or the database — and because the comparison it enables is the
    one that matters: a revision in the dump that the code has never heard of
    means the dump is from the future, and restoring it loses data silently.
    An empty tuple means the caller could not establish the order, which is
    itself reported rather than treated as agreement.
    """
    findings: list[RestoreFinding] = []
    origin = manifest.origin

    findings.extend(_format_findings(manifest, supported_format_major))
    findings.extend(_completeness_findings(manifest))
    findings.extend(_schema_findings(origin, target, migration_order))
    findings.extend(_extension_findings(origin, target))
    findings.extend(_server_findings(origin, target))
    findings.extend(_storage_findings(origin, target))
    findings.extend(_destination_findings(target_relation_count, clean_requested))
    findings.extend(_secret_findings(manifest))

    identity_findings, installation_id, may_publish = _identity(
        manifest,
        target=target,
        intent=intent,
        new_installation_id=new_installation_id,
        origin_is_fenced=origin_is_fenced,
    )
    findings.extend(identity_findings)

    migrations_required = _schema_is_behind(origin.schema_revision, target, migration_order)
    return RestoreVerdict(
        intent=intent,
        findings=tuple(findings),
        installation_id=installation_id,
        may_publish=may_publish and not any(finding.blocks for finding in findings),
        migrations_required=migrations_required,
    )


# ── the individual rules ───────────────────────────────────────────────────


def _format_findings(manifest: BackupManifest, supported_major: int) -> list[RestoreFinding]:
    if manifest.format_name != "lab-assistant-recovery":
        return [
            RestoreFinding(
                code="format_unknown",
                severity=Severity.BLOCKING,
                detail=(f"this is a {manifest.format_name!r} manifest, not a recovery backup"),
                remedy="a library archive is imported, not restored; see the archive format",
            )
        ]
    if manifest.format_major > supported_major:
        return [
            RestoreFinding(
                code="format_too_new",
                severity=Severity.BLOCKING,
                detail=(
                    f"backup format {manifest.format_version} is newer than the "
                    f"{supported_major}.x this code can read"
                ),
                remedy="restore with the application version that wrote the backup",
            )
        ]
    if manifest.format_version != "1.0" and manifest.format_major == supported_major:
        # A newer minor only added fields. Restorable, and worth saying out loud,
        # because anything this code did not understand is not in the restore.
        return [
            RestoreFinding(
                code="format_newer_minor",
                severity=Severity.ADVISORY,
                detail=(
                    f"backup format {manifest.format_version} is a newer minor than 1.0; "
                    "fields this version does not know are not interpreted"
                ),
            )
        ]
    return []


def _completeness_findings(manifest: BackupManifest) -> list[RestoreFinding]:
    findings: list[RestoreFinding] = []
    for item in manifest.incomplete:
        findings.append(
            RestoreFinding(
                code="backup_incomplete",
                severity=Severity.BLOCKING,
                detail=f"the backup records itself as incomplete: {item}",
                remedy="take a complete backup; a known hole is not a recovery point",
            )
        )
    if manifest.component(ComponentKind.DATABASE_DUMP) is None:
        findings.append(
            RestoreFinding(
                code="no_database_dump",
                severity=Severity.BLOCKING,
                detail="the backup has no database dump; there is nothing to restore into",
            )
        )
    if manifest.component(ComponentKind.OBJECT_PAYLOADS) is None:
        findings.append(
            RestoreFinding(
                code="no_object_payloads",
                severity=Severity.ADVISORY,
                detail=(
                    "the backup carries no object payloads: the database will restore and "
                    "every citation into it will resolve only if the original store is "
                    "still reachable"
                ),
                remedy="confirm the object store survived, or restore from one that has it",
            )
        )
    return findings


def _schema_findings(
    origin: InstallationFingerprint,
    target: InstallationFingerprint,
    migration_order: tuple[str, ...],
) -> list[RestoreFinding]:
    if origin.schema_revision is None:
        return [
            RestoreFinding(
                code="schema_revision_absent",
                severity=Severity.ADVISORY,
                detail=(
                    "the backup records no Alembic revision: its database was created by "
                    "`lab db create` rather than by migrations"
                ),
                remedy="stamp the restored database before applying further migrations",
            )
        ]
    if not migration_order:
        return [
            RestoreFinding(
                code="migration_order_unknown",
                severity=Severity.ADVISORY,
                detail=(
                    f"cannot place the backup's revision {origin.schema_revision} in this "
                    "application's migration history, so forward/backward could not be "
                    "established"
                ),
                remedy="run the preflight where the migration scripts are readable",
            )
        ]
    if origin.schema_revision not in migration_order:
        return [
            RestoreFinding(
                code="schema_from_the_future",
                severity=Severity.BLOCKING,
                detail=(
                    f"the backup is at revision {origin.schema_revision}, which this "
                    "application has never heard of: the dump is from a newer schema"
                ),
                remedy=(
                    "restore with the application version that wrote it; older code "
                    "restores the rows and then ignores the columns it does not know"
                ),
            )
        ]
    if target.schema_revision and target.schema_revision not in migration_order:
        return [
            RestoreFinding(
                code="target_schema_unknown",
                severity=Severity.ADVISORY,
                detail=(
                    f"the destination is at revision {target.schema_revision}, which is not "
                    "in this application's history"
                ),
            )
        ]
    return []


def _schema_is_behind(
    revision: str | None,
    target: InstallationFingerprint,
    migration_order: tuple[str, ...],
) -> bool:
    """Is the dump's schema older than the code's head, so migrations must run?"""
    if revision is None or not migration_order or revision not in migration_order:
        return False
    return migration_order.index(revision) < len(migration_order) - 1


def _extension_findings(
    origin: InstallationFingerprint, target: InstallationFingerprint
) -> list[RestoreFinding]:
    findings: list[RestoreFinding] = []
    for name, version in sorted(origin.extensions.items()):
        present = target.extensions.get(name)
        if present is None:
            findings.append(
                RestoreFinding(
                    code="extension_missing",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"the backup needs extension {name} {version} and the destination "
                        "does not have it"
                    ),
                    remedy=f"install {name} before restoring; the dump will not replay without it",
                )
            )
            continue
        if _version_tuple(present) < _version_tuple(version):
            findings.append(
                RestoreFinding(
                    code="extension_older",
                    severity=Severity.BLOCKING,
                    detail=(
                        f"the destination has {name} {present}, older than the {version} "
                        "the backup was taken with"
                    ),
                    remedy=(
                        f"upgrade {name} to {version} or later; an index written by a newer "
                        "version is not readable by an older one"
                    ),
                )
            )
        elif _version_tuple(present) > _version_tuple(version):
            findings.append(
                RestoreFinding(
                    code="extension_newer",
                    severity=Severity.ADVISORY,
                    detail=f"the destination has {name} {present}, newer than {version}",
                    remedy="check the extension's own upgrade notes before serving",
                )
            )
    return findings


def _server_findings(
    origin: InstallationFingerprint, target: InstallationFingerprint
) -> list[RestoreFinding]:
    origin_major = _version_tuple(origin.database_server_version)[:1]
    target_major = _version_tuple(target.database_server_version)[:1]
    if not origin_major or not target_major:
        return [
            RestoreFinding(
                code="server_version_unreadable",
                severity=Severity.ADVISORY,
                detail=(
                    f"cannot compare PostgreSQL {origin.database_server_version!r} with "
                    f"{target.database_server_version!r}"
                ),
            )
        ]
    if target_major < origin_major:
        return [
            RestoreFinding(
                code="server_older",
                severity=Severity.BLOCKING,
                detail=(
                    f"the destination runs PostgreSQL {target.database_server_version}, older "
                    f"than the {origin.database_server_version} that took the dump"
                ),
                remedy="restore into the same major version or newer; pg_restore does not go back",
            )
        ]
    if target_major > origin_major:
        return [
            RestoreFinding(
                code="server_newer",
                severity=Severity.ADVISORY,
                detail=(
                    f"the destination runs PostgreSQL {target.database_server_version}, newer "
                    f"than {origin.database_server_version}: supported, and worth recording "
                    "in the drill notes"
                ),
            )
        ]
    return []


def _storage_findings(
    origin: InstallationFingerprint, target: InstallationFingerprint
) -> list[RestoreFinding]:
    if origin.object_store_kind == target.object_store_kind:
        if origin.object_store_address != target.object_store_address:
            return [
                RestoreFinding(
                    code="object_store_moved",
                    severity=Severity.ADVISORY,
                    detail=(
                        f"the destination's store is at {target.object_store_address!r}, not "
                        f"{origin.object_store_address!r}: the payloads in this backup must be "
                        "restored into it before any citation resolves"
                    ),
                )
            ]
        return []
    return [
        RestoreFinding(
            code="object_store_kind_changed",
            severity=Severity.ADVISORY,
            detail=(
                f"the backup came from a {origin.object_store_kind} store and the destination "
                f"is {target.object_store_kind}"
            ),
            remedy=(
                "the payloads restore through the same port either way; verify with "
                "`lab storage scan --verify digest` before serving"
            ),
        )
    ]


def _destination_findings(relation_count: int, clean_requested: bool) -> list[RestoreFinding]:
    """A logical restore goes into an empty database.

    Replaying a dump over existing tables fails on the first ``CREATE TABLE``,
    and it fails *partway* — after the restore has already copied every object
    payload into the destination's store. So the destination is checked before
    anything moves, rather than discovered by `pg_restore` halfway through.

    ``--clean`` is the deliberate alternative: it drops the existing objects
    first. It is a separate word because it destroys whatever is there, and the
    operator who meant a restore into a scratch database and typed the address of
    a live one should be stopped, not accommodated.
    """
    if relation_count <= 0 or clean_requested:
        return []
    return [
        RestoreFinding(
            code="destination_not_empty",
            severity=Severity.BLOCKING,
            detail=(
                f"the destination database already holds {relation_count} table(s): a "
                "logical restore replays into an empty one"
            ),
            remedy=(
                "restore into a fresh database, or pass --clean to drop what is there "
                "first — which destroys it"
            ),
        )
    ]


def _secret_findings(manifest: BackupManifest) -> list[RestoreFinding]:
    required = [reference for reference in manifest.secret_references if reference.required]
    if not required:
        return []
    return [
        RestoreFinding(
            code="secrets_required",
            severity=Severity.ADVISORY,
            detail=(
                f"{len(required)} secret(s) must be in place before the restored installation "
                f"serves: {', '.join(reference.name for reference in required)}"
            ),
            remedy="take them from the secret manager; they are deliberately not in the backup",
        )
    ]


def _identity(
    manifest: BackupManifest,
    *,
    target: InstallationFingerprint,
    intent: RestoreIntent,
    new_installation_id: UUID | None,
    origin_is_fenced: bool,
) -> tuple[list[RestoreFinding], UUID | None, bool]:
    """Who the restored installation is, and whether it may publish.

    This is the rule the shell scripts had no way to express. A backup is a
    faithful copy, so restoring it always produces something that believes it is
    the publisher. Deciding that it is not is the operator's call, and the only
    safe default is the one that does not create a second writer.
    """
    origin = manifest.origin
    findings: list[RestoreFinding] = []

    if intent is RestoreIntent.CLONE:
        identity = new_installation_id or target.installation_id
        findings.append(
            RestoreFinding(
                code="clone_may_not_publish",
                severity=Severity.ADVISORY,
                detail=(
                    f"restored as a clone of installation {origin.installation_id}: it runs as "
                    f"{identity} and may not publish"
                ),
                remedy=(
                    "promote it under the handover protocol once the original is fenced; "
                    "restoring does not transfer the right to publish"
                ),
            )
        )
        if identity == origin.installation_id:
            findings.append(
                RestoreFinding(
                    code="clone_keeps_origin_identity",
                    severity=Severity.BLOCKING,
                    detail=(
                        "a clone was asked to run under the origin's own installation id, "
                        f"{origin.installation_id}"
                    ),
                    remedy="allocate a new installation id, or restore with the replace intent",
                )
            )
        return findings, identity, False

    # REPLACE: the operator asserts the original is gone or demoted.
    if not origin_is_fenced:
        findings.append(
            RestoreFinding(
                code="origin_not_fenced",
                severity=Severity.BLOCKING,
                detail=(
                    f"this restore would take over installation {origin.installation_id} and "
                    "publish as it, and the original has not been declared fenced"
                ),
                remedy=(
                    "stop the original installation, then re-run asserting it is fenced; two "
                    "publishers on one library diverge silently"
                ),
            )
        )
        return findings, origin.installation_id, False

    findings.append(
        RestoreFinding(
            code="writer_taken_over",
            severity=Severity.ADVISORY,
            detail=(
                f"taking over installation {origin.installation_id} at writer epoch "
                f"{origin.writer_epoch + 1}; the original must never be started again"
            ),
            remedy="record the fencing in the handover log before enabling workers",
        )
    )
    return findings, origin.installation_id, True


def _version_tuple(value: str) -> tuple[int, ...]:
    """Leading numeric components of a version string.

    `16.13`, `0.8.2`, and PostgreSQL's chatty `16.13 (Debian 16.13-1.pgdg120+1)`
    all reduce to the numbers that decide compatibility. A string with no
    leading number yields an empty tuple, which callers treat as "unreadable"
    rather than as zero — `0.0` would compare as older than everything and
    produce a confident wrong answer.
    """
    parts: list[int] = []
    for chunk in value.strip().split(" ")[0].split("."):
        digits = ""
        for character in chunk:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)
