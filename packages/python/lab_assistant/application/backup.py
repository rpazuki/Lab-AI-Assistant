"""Private portable backups: taking one, checking one, and putting one back.

Three things must survive a lost VM — the control plane, the object store, and
the secrets — and the predecessor's scripts covered one and a half of them. They
took a `pg_basebackup` and an `rsync` of a local directory, which is not a backup
of an S3 installation at all, and they recorded nothing about the installation
that would let a restore establish compatibility before it started destroying
the destination.

This is the replacement, and its shape follows from what a restore has to be
able to prove:

- **The payloads come through the `ObjectStore` port**, into a filesystem store
  rooted inside the backup directory. So a bucket backs up exactly as a local
  directory does, through the same verified, resumable copy that
  `lab storage migrate` uses — because they are the same operation pointed at a
  different destination, and writing a second one would mean maintaining two
  copies of the hard part.
- **The dump is logical.** A physical `PGDATA` copy is faster and only restores
  inside a narrow platform envelope. The contract here is portability to a VM
  nobody has provisioned yet, so the portable method is the one that is tested.
- **Every component is digested at write time**, and the manifest records the
  counts a person reads out loud during a drill.
- **Secrets are listed and not carried.** What travels is the checklist of what
  the restored installation will need and where it comes from.
- **A restore decides what it is before it touches anything.** The compatibility
  rules live in `domain/recovery.py`; this service's job is to gather the two
  fingerprints honestly and refuse when the verdict says to.

What it deliberately does not do: encrypt. A backup contains password hashes,
unreviewed evidence and chat history, so it must live in encrypted storage — and
a format that rolled its own encryption would be worse than one that states the
requirement and leaves it to storage that is actually audited.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.pagination import DEFAULT_PAGE_SIZE, iter_pages
from lab_assistant.application.ports import DatabaseSnapshot, ObjectStore
from lab_assistant.application.storage_migration import (
    MigrationReport,
    StorageMigrationService,
    VerifyLevel,
)
from lab_assistant.config import Settings
from lab_assistant.contracts.backup import (
    BackupComponent,
    BackupManifest,
    ComponentKind,
    InstallationFingerprint,
    SecretReference,
    WorkspaceInventory,
)
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.domain.recovery import (
    RestoreIntent,
    RestoreVerdict,
    evaluate_restore,
)

__all__ = [
    "MANIFEST_DIGEST_NAME",
    "MANIFEST_NAME",
    "BackupReport",
    "BackupService",
    "RestoreReport",
    "VerificationReport",
    "installation_identity",
    "secret_references",
]

MANIFEST_NAME = "MANIFEST.json"
MANIFEST_DIGEST_NAME = "MANIFEST.sha256"
DUMP_RELATIVE = "database/dump.pgcustom"
OBJECTS_RELATIVE = "objects"
INVENTORY_RELATIVE = "objects-inventory.json"

#: A store rooted at a path. Injected rather than imported: the filesystem store
#: is an adapter, and the application layer may not reach into adapters.
ObjectStoreFactory = Callable[[Path], ObjectStore]

#: Things a person will look for in the backup and not find. Writing them down
#: is the difference between a decision and a bug: "the model cache is not in
#: here" is a sentence somebody needs to read before they spend an hour on it.
EXCLUDED = (
    ".env and every value in it — the secret manager holds those, and an archive "
    "carrying the database password makes every off-site copy a credential leak",
    "the embedding model cache — re-downloadable, and verified by digest on arrival",
    "rotated container logs and support bundles — diagnostic, not corpus authority",
    "nginx configuration, the TLS chain and its private key — reissued for the "
    "destination host, not copied to it",
)


@dataclass(frozen=True, slots=True)
class BackupReport:
    backup_id: UUID
    directory: str
    dry_run: bool
    manifest: BackupManifest | None = None
    objects: MigrationReport | None = None
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, object]:
        return {
            "backup_id": str(self.backup_id),
            "directory": self.directory,
            "dry_run": self.dry_run,
            "manifest": self.manifest.model_dump(mode="json") if self.manifest else None,
            "objects": self.objects.as_dict() if self.objects else None,
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    directory: str
    manifest: BackupManifest | None = None
    components_checked: int = 0
    objects_checked: int = 0
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems

    def as_dict(self) -> dict[str, object]:
        return {
            "directory": self.directory,
            "backup_id": str(self.manifest.backup_id) if self.manifest else None,
            "created_at": self.manifest.created_at.isoformat() if self.manifest else None,
            "components_checked": self.components_checked,
            "objects_checked": self.objects_checked,
            "problems": list(self.problems),
            "verified": self.ok,
        }


@dataclass(frozen=True, slots=True)
class RestoreReport:
    directory: str
    verdict: RestoreVerdict | None = None
    verification: VerificationReport | None = None
    applied: bool = False
    objects: MigrationReport | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)
    #: What the operator still has to do. A restore that silently considered
    #: itself finished is how an installation ends up serving before anybody
    #: ran `lab db verify-restore`.
    next_steps: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems and (self.verdict is None or self.verdict.permitted)

    def as_dict(self) -> dict[str, object]:
        return {
            "directory": self.directory,
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "verification": self.verification.as_dict() if self.verification else None,
            "applied": self.applied,
            "objects": self.objects.as_dict() if self.objects else None,
            "problems": list(self.problems),
            "next_steps": list(self.next_steps),
        }


def installation_identity(settings: Settings) -> tuple[UUID, str]:
    """Who this installation is, and whether anybody said so.

    A configured `LAB_INSTALLATION_ID` is the answer when there is one. There
    usually is not, on a deployment that predates this command, and refusing to
    back up until somebody sets one would make the safe path the inconvenient
    one.

    So the fallback derives an id from the installation's own addresses — its
    database and its object store. That has exactly the property the one-writer
    rule needs: a restore onto a new host, pointed at a new database and a new
    bucket, derives a *different* id and is therefore recognised as the clone it
    is. It has one weakness, which is why the manifest records which case
    applies: an installation that deliberately changes address keeps its data
    and loses its derived identity, so a planned move that intends to keep
    publishing configures an id before it moves.
    """
    if settings.installation_id is not None:
        return settings.installation_id, "configured"
    seed = "|".join(
        (
            settings.env.value,
            _database_address(settings),
            settings.object_store_kind,
            _store_address(settings) or "",
        )
    )
    return uuid5(NAMESPACE_URL, f"lab-assistant-installation:{seed}"), "derived"


def _database_address(settings: Settings) -> str:
    """Host, port and database name — never the credentials in the URL."""
    parts = urlsplit(settings.database_url.replace("+asyncpg", ""))
    return f"{parts.hostname or 'localhost'}:{parts.port or 5432}{parts.path}"


def secret_references(settings: Settings) -> tuple[SecretReference, ...]:
    """What this installation will need from the secret manager to come back.

    Derived from the configuration that is actually loaded, not from a fixed
    list: an installation on a filesystem store must not be told to go and find
    S3 credentials, and one with the external model enabled must be. A checklist
    that is wrong in either direction stops being read.
    """
    references = [
        SecretReference(
            name="LAB_SECRET_KEY",
            where="the institution's secret manager",
            note=(
                "restoring a different value invalidates every issued session, which is "
                "sometimes exactly what a recovery wants — decide deliberately"
            ),
        ),
        SecretReference(
            name="LAB_DATABASE_URL",
            where="the institution's secret manager",
            note="carries the database password; the restored host's address, not the old one",
        ),
    ]
    if settings.object_store_kind == "s3":
        references.append(
            SecretReference(
                name="LAB_OBJECT_STORE_ACCESS_KEY / LAB_OBJECT_STORE_SECRET_KEY",
                where="the institution's identity provider, or an instance role",
                note=(
                    "a restored installation should get its own credential, not a copy of "
                    "the original's: revoking one must not disable the other"
                ),
            )
        )
    if settings.external_model_enabled:
        references.append(
            SecretReference(
                name="LAB_LLM_API_KEY",
                where="the provider's console, through the secret manager",
                required=False,
                note="a restore can serve retrieval and citations without it",
            )
        )
    references.append(
        SecretReference(
            name="TLS certificate and private key",
            where="reissued for the destination hostname",
            note="copying the original host's certificate is not a restore step",
        )
    )
    return tuple(references)


class BackupService:
    """Takes a private recovery backup, verifies one, and puts one back."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        snapshot: DatabaseSnapshot,
        installation_id: UUID,
        store_factory: ObjectStoreFactory,
        writer_epoch: int = 0,
        identity_source: str = "derived",
    ) -> None:
        self._context = context
        self._snapshot = snapshot
        self._installation_id = installation_id
        self._store_factory = store_factory
        self._writer_epoch = writer_epoch
        self._identity_source = identity_source

    # ── create ─────────────────────────────────────────────────────────────

    async def create(
        self,
        directory: Path,
        *,
        include_objects: bool = True,
        dry_run: bool = False,
        verify: VerifyLevel = VerifyLevel.DIGEST,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> BackupReport:
        """Write a complete recovery backup into ``directory``.

        The order is deliberate: fingerprint, then objects, then the dump, then
        the manifest. Objects before the database so the store is never *behind*
        the control plane — a row citing an object the store does not have is a
        broken citation, an object nothing cites is harmless. The predecessor's
        script had the opposite order under a comment claiming this one.
        """
        backup_id = uuid4()
        directory = Path(directory)
        problems: list[str] = []
        incomplete: list[str] = []

        fingerprint = await self._fingerprint()
        workspaces = await self._workspace_inventory(page_size=page_size)

        if dry_run:
            manifest = BackupManifest(
                backup_id=backup_id,
                created_at=self._context.now(),
                origin=fingerprint,
                components=(),
                secret_references=secret_references(self._context.settings),
                workspaces=workspaces,
                excluded=EXCLUDED,
                incomplete=("dry run: nothing was written",),
            )
            return BackupReport(
                backup_id=backup_id,
                directory=str(directory),
                dry_run=True,
                manifest=manifest,
            )

        directory.mkdir(parents=True, exist_ok=True)
        _refuse_to_overwrite(directory)

        components: list[BackupComponent] = []
        objects_report: MigrationReport | None = None

        if include_objects:
            objects_report = await self._copy_objects(directory, verify=verify, page_size=page_size)
            components.append(_objects_component(directory, objects_report))
            components.append(_inventory_component(directory, objects_report))
            if objects_report.problems:
                incomplete.append(
                    f"{len(objects_report.problems)} object(s) could not be copied; see "
                    f"{INVENTORY_RELATIVE}"
                )
        else:
            incomplete.append(
                "object payloads were not included: the database alone does not hold the bytes"
            )

        dump = await self._snapshot.dump(directory / DUMP_RELATIVE)
        components.append(
            BackupComponent(
                name="control plane",
                kind=ComponentKind.DATABASE_DUMP,
                path=DUMP_RELATIVE,
                sha256=dump.sha256,
                byte_length=dump.byte_length,
                detail={"tool_version": dump.tool_version},
            )
        )

        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=self._context.now(),
            origin=fingerprint,
            components=tuple(components),
            secret_references=secret_references(self._context.settings),
            workspaces=workspaces,
            excluded=EXCLUDED,
            incomplete=tuple(incomplete),
        )
        _write_manifest(directory, manifest)

        return BackupReport(
            backup_id=backup_id,
            directory=str(directory),
            dry_run=False,
            manifest=manifest,
            objects=objects_report,
            problems=tuple(problems),
        )

    # ── verify ─────────────────────────────────────────────────────────────

    async def verify(self, directory: Path, *, check_objects: bool = True) -> VerificationReport:
        """Re-read a backup and confirm it is the one that was written.

        Reads only the backup: it never touches the running installation. That
        is what makes it runnable against an off-site copy, which is the only
        copy whose integrity is actually in question.
        """
        directory = Path(directory)
        problems: list[str] = []

        manifest = _read_manifest(directory, problems)
        if manifest is None:
            return VerificationReport(directory=str(directory), problems=tuple(problems))

        recorded = directory / MANIFEST_DIGEST_NAME
        if recorded.is_file():
            expected = recorded.read_text(encoding="utf-8").split()[0]
            if expected != manifest.manifest_sha256():
                problems.append(
                    f"the manifest digests to {manifest.manifest_sha256()[:12]}… and "
                    f"{MANIFEST_DIGEST_NAME} records {expected[:12]}…: the manifest was "
                    "edited after it was written"
                )
        else:
            problems.append(f"{MANIFEST_DIGEST_NAME} is absent")

        for item in manifest.incomplete:
            problems.append(f"the backup records itself as incomplete: {item}")

        checked = 0
        objects_checked = 0
        for component in manifest.components:
            path = directory / component.path
            if not path.exists():
                if not component.optional:
                    problems.append(f"{component.path} is named in the manifest and absent")
                continue
            if component.kind is ComponentKind.OBJECT_PAYLOADS:
                if check_objects:
                    objects_checked = await self._verify_objects(directory, component, problems)
                checked += 1
                continue
            digest = _digest_file(path)
            size = path.stat().st_size
            if size != component.byte_length:
                problems.append(
                    f"{component.path} is {size} bytes, the manifest records "
                    f"{component.byte_length}"
                )
            elif digest != component.sha256:
                problems.append(
                    f"{component.path} digests to {digest[:12]}…, the manifest records "
                    f"{component.sha256[:12]}…"
                )
            checked += 1

        return VerificationReport(
            directory=str(directory),
            manifest=manifest,
            components_checked=checked,
            objects_checked=objects_checked,
            problems=tuple(problems),
        )

    # ── restore ────────────────────────────────────────────────────────────

    async def restore(
        self,
        directory: Path,
        *,
        intent: RestoreIntent = RestoreIntent.CLONE,
        apply: bool = False,
        origin_is_fenced: bool = False,
        new_installation_id: UUID | None = None,
        migration_order: tuple[str, ...] = (),
        clean: bool = False,
        verify: VerifyLevel = VerifyLevel.DIGEST,
    ) -> RestoreReport:
        """Preflight a restore, and — with ``apply`` — carry it out.

        Without ``apply`` this writes nothing and changes nothing: it verifies
        the backup, fingerprints the destination, and returns the verdict. That
        is the step the predecessor's script had no equivalent of, and it is the
        one that catches a missing extension while it is still cheap to install.
        """
        directory = Path(directory)
        verification = await self.verify(directory, check_objects=False)
        if verification.manifest is None:
            return RestoreReport(
                directory=str(directory),
                verification=verification,
                problems=verification.problems,
            )

        target = await self._fingerprint()
        database = await self._snapshot.fingerprint()
        verdict = evaluate_restore(
            verification.manifest,
            target=target,
            intent=intent,
            new_installation_id=new_installation_id,
            origin_is_fenced=origin_is_fenced,
            migration_order=migration_order,
            target_relation_count=database.relation_count,
            clean_requested=clean,
        )

        problems = list(verification.problems)
        problems.extend(finding.detail for finding in verdict.blocking)

        if not apply or problems:
            return RestoreReport(
                directory=str(directory),
                verdict=verdict,
                verification=verification,
                applied=False,
                problems=tuple(problems),
                next_steps=_next_steps(verdict, applied=False),
            )

        dump = verification.manifest.component(ComponentKind.DATABASE_DUMP)
        if dump is None:  # pragma: no cover - the verdict already blocked on this
            problems.append("no database dump in the backup")
            return RestoreReport(
                directory=str(directory),
                verdict=verdict,
                verification=verification,
                problems=tuple(problems),
            )

        # The control plane first, and the order is forced rather than chosen.
        # The object copy is driven by the *destination's* registry: it copies
        # what the restored installation actually references, and verifies each
        # one against the digest that installation recorded. Before the dump is
        # replayed there is no registry — on a fresh database there is not even
        # a table — so a copy run first would either fail or, worse, succeed
        # having copied nothing.
        #
        # `create` orders these the other way, and that is also right: a backup
        # must never hold a control plane newer than its payloads. Restoring is
        # not the mirror image of backing up, which is the thing that is easy to
        # get wrong here.
        await self._snapshot.restore(directory / dump.path, clean=clean)

        objects_report = await self._restore_objects(
            directory, verification.manifest, verify=verify
        )
        if objects_report is not None and objects_report.problems:
            # The database is restored and some payloads are not, which is the
            # broken-citation state. It is reported as a problem and the restore
            # is *not* marked applied, because what it needs is the object step
            # run again — not an operator concluding it is finished.
            problems.append(
                f"{len(objects_report.problems)} object(s) did not restore: the control "
                "plane is in place and those payloads are not, so citations into them "
                "will not resolve"
            )
            return RestoreReport(
                directory=str(directory),
                verdict=verdict,
                verification=verification,
                applied=False,
                objects=objects_report,
                problems=tuple(problems),
                next_steps=(
                    "re-run the restore: the database is already in place, and the object "
                    "copy resumes by skipping what arrived",
                    "if a payload cannot be produced at all, `lab db verify-restore` names "
                    "every release that cites it",
                ),
            )

        return RestoreReport(
            directory=str(directory),
            verdict=verdict,
            verification=verification,
            applied=True,
            objects=objects_report,
            problems=(),
            next_steps=_next_steps(verdict, applied=True),
        )

    # ── internals ──────────────────────────────────────────────────────────

    async def _fingerprint(self) -> InstallationFingerprint:
        database = await self._snapshot.fingerprint()
        settings = self._context.settings
        return InstallationFingerprint(
            installation_id=self._installation_id,
            identity_source=self._identity_source,
            env=settings.env.value,
            app_version=_app_version(),
            schema_revision=database.schema_revision,
            database_server_version=database.server_version,
            extensions=dict(database.extensions),
            object_store_kind=settings.object_store_kind,
            object_store_address=_store_address(settings),
            writer_epoch=self._writer_epoch,
        )

    async def _workspace_inventory(self, *, page_size: int) -> tuple[WorkspaceInventory, ...]:
        """Counts per workspace, paged.

        Keyset-paged rather than `limit`: this is a lifecycle path that must see
        every row, and a page size is not a completeness control. A backup whose
        counts silently stopped at a thousand objects would report an agreement
        it had not checked.
        """
        rows: list[WorkspaceInventory] = []
        async with self._context.uow_factory() as uow:
            async for workspace in iter_pages(
                lambda after, size: uow.workspaces.list_workspaces(limit=size, after_id=after),
                page_size=page_size,
            ):
                objects = 0
                object_bytes = 0
                async for record in iter_pages(
                    lambda after, size, scope=workspace.id: uow.objects.list_objects(
                        scope, limit=size, after_id=after
                    ),
                    page_size=page_size,
                ):
                    objects += 1
                    object_bytes += record.byte_length
                releases = 0
                async for _release in iter_pages(
                    lambda after, size, scope=workspace.id: uow.releases.list_all_releases(
                        scope, limit=size, after_id=after
                    ),
                    page_size=page_size,
                ):
                    releases += 1
                rows.append(
                    WorkspaceInventory(
                        workspace_id=workspace.id,
                        slug=workspace.slug,
                        releases=releases,
                        objects=objects,
                        object_bytes=object_bytes,
                    )
                )
        return tuple(rows)

    async def _copy_objects(
        self, directory: Path, *, verify: VerifyLevel, page_size: int
    ) -> MigrationReport:
        destination = self._store_factory(directory / OBJECTS_RELATIVE)
        service = StorageMigrationService(self._context, destination=destination)
        report = await service.migrate(
            dry_run=False,
            verify=verify,
            # A payload the database does not describe is usually a rolled-back
            # upload. It goes into the backup anyway: deciding it is disposable
            # is a retention decision, and a backup is the wrong place to make
            # one silently.
            include_source_only=True,
            page_size=page_size,
        )
        (directory / INVENTORY_RELATIVE).write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        await _close(destination)
        return report

    async def _restore_objects(
        self, directory: Path, manifest: BackupManifest, *, verify: VerifyLevel
    ) -> MigrationReport | None:
        """Copy the backup's payloads into the configured store.

        The same migration, pointed the other way: the backup is the source and
        the running installation's store is the destination. That is why an S3
        installation restores from a backup taken on a filesystem one without a
        separate code path.

        It is driven by the destination's object registry, which is why it runs
        *after* the dump has been replayed. That is the useful direction: it
        copies what the restored control plane actually references, verifies
        each payload against the digest that control plane recorded, and carries
        anything else the backup holds besides.
        """
        if manifest.component(ComponentKind.OBJECT_PAYLOADS) is None:
            return None
        source = self._store_factory(directory / OBJECTS_RELATIVE)
        service = StorageMigrationService(
            self._context, destination=self._context.object_store, source=source
        )
        report = await service.migrate(dry_run=False, verify=verify, include_source_only=True)
        await _close(source)
        return report

    async def _verify_objects(
        self, directory: Path, component: BackupComponent, problems: list[str]
    ) -> int:
        """Re-digest every payload in the backup's own store.

        This is the expensive half of a verify and the half that catches bit
        rot in off-site storage, which is the failure that a backup nobody has
        read is most likely to be hiding.
        """
        store = self._store_factory(directory / component.path)
        checked = 0
        inventory = getattr(store, "inventory", None)
        if inventory is None:  # pragma: no cover - the filesystem store enumerates
            problems.append(f"{component.path} cannot be enumerated, so it cannot be verified")
            await _close(store)
            return 0
        async for stored in inventory():
            checked += 1
            try:
                if not await store.verify(stored.id):
                    problems.append(
                        f"{component.path}: object {stored.id} does not match its digest"
                    )
            except LabAssistantError as exc:
                problems.append(f"{component.path}: object {stored.id} is unreadable ({exc})")
        if component.item_count is not None and checked != component.item_count:
            problems.append(
                f"{component.path} holds {checked} object(s), the manifest records "
                f"{component.item_count}"
            )
        await _close(store)
        return checked


# ── helpers ────────────────────────────────────────────────────────────────


def _next_steps(verdict: RestoreVerdict, *, applied: bool) -> tuple[str, ...]:
    steps: list[str] = []
    if not verdict.permitted:
        steps.append(
            "this restore is refused: every blocking finding must be resolved, not overridden"
        )
        return tuple(steps)
    if not applied:
        steps.append("re-run with --apply to carry the restore out")
        return tuple(steps)
    if verdict.migrations_required:
        steps.append("run `alembic upgrade head`: the dump is behind this application's schema")
    steps.append("put the secrets back from the secret manager; they are not in the backup")
    steps.append(
        "run `lab db verify-restore --workspace <slug>` before serving: a restore that has "
        "not been verified is not a restore"
    )
    if not verdict.may_publish:
        steps.append(
            "this installation may not publish: promote it under the handover protocol once "
            "the original is fenced"
        )
    return tuple(steps)


def _refuse_to_overwrite(directory: Path) -> None:
    """A backup never writes into a directory that already holds one.

    Each run gets its own directory. Merging into an existing backup would
    produce a manifest describing one run and payloads from two, and the
    resulting mixture verifies as long as nobody looks closely.
    """
    if (directory / MANIFEST_NAME).exists():
        raise LabAssistantError(
            "that directory already holds a backup",
            context={
                "directory": str(directory),
                "why": "merging two runs produces a manifest that describes neither",
                "fix": "choose a new directory; each backup is its own timestamped run",
            },
        ).with_class(ErrorClass.CONFLICT)


def _write_manifest(directory: Path, manifest: BackupManifest) -> None:
    payload = canonical_json(manifest.model_dump(mode="python"))
    (directory / MANIFEST_NAME).write_bytes(payload)
    (directory / MANIFEST_DIGEST_NAME).write_text(
        f"{manifest.manifest_sha256()}  {MANIFEST_NAME}\n", encoding="utf-8"
    )


def _read_manifest(directory: Path, problems: list[str]) -> BackupManifest | None:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        problems.append(f"{MANIFEST_NAME} is absent: this directory is not a backup")
        return None
    try:
        return BackupManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        # A manifest that will not parse is the finding, not a traceback — the
        # same rule `verify-restore` learned when one bad release hid every
        # finding after it.
        problems.append(f"{MANIFEST_NAME} does not parse ({exc})")
        return None


def _objects_component(directory: Path, report: MigrationReport) -> BackupComponent:
    """Digest an object directory by its inventory, not by its bytes.

    The payloads are individually content-addressed and were verified on the way
    in. Concatenating a corpus to produce one number would make every verify
    cost a full read, and would still prove nothing the per-object digests do
    not already prove.
    """
    listing = sorted(
        (str(path.relative_to(directory)), path.stat().st_size)
        for path in (directory / OBJECTS_RELATIVE).rglob("*")
        if path.is_file()
    )
    return BackupComponent(
        name="object payloads",
        kind=ComponentKind.OBJECT_PAYLOADS,
        path=OBJECTS_RELATIVE,
        sha256=sha256_bytes(canonical_json(listing)),
        byte_length=sum(size for _, size in listing),
        item_count=report.copied + report.skipped_present,
        detail={
            "verify": report.verify.value,
            "tombstoned_not_copied": len(report.plan.tombstoned_not_copied),
        },
    )


def _inventory_component(directory: Path, report: MigrationReport) -> BackupComponent:
    path = directory / INVENTORY_RELATIVE
    return BackupComponent(
        name="object inventory",
        kind=ComponentKind.OBJECT_INVENTORY,
        path=INVENTORY_RELATIVE,
        sha256=_digest_file(path),
        byte_length=path.stat().st_size,
        item_count=report.plan.objects,
    )


def _store_address(settings: Settings) -> str | None:
    """Where the store is, with nothing secret in it."""
    if settings.object_store_kind == "filesystem":
        return str(settings.object_store_root)
    bucket = settings.object_store_bucket
    if bucket:
        prefix = settings.object_store_prefix
        return f"{bucket}/{prefix}" if prefix else bucket
    return None


def _app_version() -> str:
    from lab_assistant import __version__

    return __version__


def _digest_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


async def _close(store: object) -> None:
    close = getattr(store, "aclose", None)
    if close is not None:
        await close()
