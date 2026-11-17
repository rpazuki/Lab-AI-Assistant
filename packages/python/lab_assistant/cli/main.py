"""The ``lab`` command line.

Operational surface for the things an operator or steward does outside the web
application: inspect configuration, create a scratch schema, run a worker, publish
or roll back a release, validate an export package, and drive the legacy importers.

Every command goes through the same application services the API uses, so a CLI
action and a UI action are the same action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from lab_assistant import __version__
from lab_assistant.config import ConfigProvenance, Settings, load_settings
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab",
        description="Lab AI Assistant operations.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config", help="inspect effective configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    print_config = config_sub.add_parser(
        "print", help="print effective configuration with per-key source"
    )
    print_config.add_argument(
        "--json", action="store_true", help="emit JSON rather than a table"
    )
    config_sub.add_parser(
        "connectors", help="which connectors this deployment has, and their limits"
    )

    database = subparsers.add_parser("db", help="database operations")
    database_sub = database.add_subparsers(dest="db_command", required=True)
    database_sub.add_parser("create", help="create the schema in a scratch database")
    database_sub.add_parser("check", help="verify the schema matches the application")
    create_admin = database_sub.add_parser(
        "create-admin", help="create the first admin and their workspace"
    )
    create_admin.add_argument("--email", required=True)
    create_admin.add_argument("--workspace", required=True, help="workspace slug")
    create_admin.add_argument("--display-name", default=None)
    verify_audit = database_sub.add_parser(
        "verify-audit", help="verify the audit hash chain for a workspace"
    )
    verify_audit.add_argument("--workspace", required=True, help="workspace slug")
    verify_restore = database_sub.add_parser(
        "verify-restore", help="post-restore integrity check for a workspace"
    )
    verify_restore.add_argument("--workspace", required=True, help="workspace slug")
    revoke_sessions = database_sub.add_parser(
        "revoke-sessions", help="invalidate every issued session for a user"
    )
    revoke_sessions.add_argument("--user", required=True, help="user id")

    storage = subparsers.add_parser("storage", help="object store and registry operations")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    scan = storage_sub.add_parser(
        "scan", help="walk every object reference and every registered object"
    )
    scan.add_argument("--workspace", required=True, help="workspace slug")
    scan.add_argument(
        "--verify",
        choices=["none", "exists", "digest"],
        default="exists",
        help="how hard to look at the bytes; 'digest' reads every object",
    )
    scan.add_argument(
        "--skip-orphans",
        action="store_true",
        help="do not hold referenced ids in memory to find unreferenced objects",
    )
    backfill = storage_sub.add_parser(
        "backfill", help="register objects the store knows and the database does not"
    )
    backfill.add_argument("--workspace", required=True, help="workspace slug")
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="write the rows; without it this reports what it would do",
    )
    migrate = storage_sub.add_parser(
        "migrate", help="copy every object to another store, verifiably and resumably"
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="copy; without it this inventories both sides and reports the plan",
    )
    migrate.add_argument(
        "--verify",
        choices=["none", "metadata", "digest"],
        default="digest",
        help="how each copy is confirmed; 'digest' reads the destination back",
    )
    migrate.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="progress file; an interrupted run resumes from it (default: var/migrations)",
    )
    migrate.add_argument(
        "--run-id", default=None, help="resume a named run rather than starting a new one"
    )
    migrate.add_argument(
        "--skip-source-only",
        action="store_true",
        help="do not carry payloads the database does not describe",
    )

    backup = subparsers.add_parser("backup", help="private portable backup of this installation")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    create_backup = backup_sub.add_parser(
        "create", help="dump the control plane and copy every object, with a manifest"
    )
    create_backup.add_argument(
        "directory", type=Path, help="a new directory; each backup is its own run"
    )
    create_backup.add_argument(
        "--dry-run",
        action="store_true",
        help="fingerprint and inventory without writing anything",
    )
    create_backup.add_argument(
        "--skip-objects",
        action="store_true",
        help="control plane only; the database alone does not hold the bytes",
    )
    create_backup.add_argument(
        "--verify",
        choices=["none", "metadata", "digest"],
        default="digest",
        help="how each copied object is confirmed; 'digest' reads the backup back",
    )
    verify_backup = backup_sub.add_parser(
        "verify", help="re-digest a backup; touches the installation not at all"
    )
    verify_backup.add_argument("directory", type=Path)
    verify_backup.add_argument(
        "--skip-objects",
        action="store_true",
        help="manifest and dump only; the object sweep reads every payload",
    )
    restore_backup = backup_sub.add_parser(
        "restore", help="preflight a restore, and with --apply carry it out"
    )
    restore_backup.add_argument("directory", type=Path)
    restore_backup.add_argument(
        "--apply",
        action="store_true",
        help="carry it out; without it this writes nothing and reports the verdict",
    )
    restore_backup.add_argument(
        "--intent",
        choices=["clone", "replace"],
        default="clone",
        help=(
            "'clone' is a second installation that may not publish; 'replace' takes over "
            "the original's identity and needs --origin-is-fenced"
        ),
    )
    restore_backup.add_argument(
        "--origin-is-fenced",
        action="store_true",
        help=(
            "assert the original installation is stopped or demoted; required by "
            "--intent replace, because two publishers on one library diverge silently"
        ),
    )
    restore_backup.add_argument(
        "--clean",
        action="store_true",
        help="drop existing objects before replaying the dump",
    )
    restore_backup.add_argument(
        "--verify",
        choices=["none", "metadata", "digest"],
        default="digest",
        help="how each restored object is confirmed",
    )

    task = subparsers.add_parser("task", help="inspect and steer queued work")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    for name, help_text in (
        ("inspect", "print one task's state, lease, and error"),
        ("cancel", "cancel one task"),
        ("retry", "re-queue a dead-lettered task"),
        ("queue", "queue depth and oldest ready task per stage"),
    ):
        sub = task_sub.add_parser(name, help=help_text)
        sub.add_argument("--workspace", required=True, help="workspace slug")
        if name != "queue":
            sub.add_argument("--task", required=True, help="task id")
        if name in ("cancel", "retry"):
            sub.add_argument("--reason", default="operator_action")

    worker = subparsers.add_parser("worker", help="run a worker loop")
    worker.add_argument(
        "worker_class",
        choices=["network", "compute"],
        help="which pool to run: network-bound or compute-bound stages",
    )
    worker.add_argument("--once", action="store_true", help="claim at most one task, then exit")
    worker.add_argument("--owner", default=None, help="lease owner name (defaults to hostname)")

    release = subparsers.add_parser("release", help="corpus release operations")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    for name, help_text in (
        ("list", "list releases and aliases"),
        ("validate", "run release gates"),
        ("publish", "switch the alias to a release"),
        ("rollback", "switch the alias back"),
        ("withdraw", "take a release permanently out of service"),
        ("retract-work", "record a retraction and plan the successor release"),
        ("references", "which releases reference an object digest"),
    ):
        sub = release_sub.add_parser(name, help=help_text)
        sub.add_argument("--workspace", required=True, help="workspace slug")
        if name in ("validate", "publish", "withdraw"):
            sub.add_argument("--release-id", required=True)
        if name in ("publish", "rollback"):
            sub.add_argument("--alias", default="current")
        if name == "retract-work":
            sub.add_argument("--work-id", required=True)
            sub.add_argument("--reason", default="publisher_retraction_notice")
        if name == "references":
            sub.add_argument("--digest", required=True, help="sha256 hex digest")
        sub.add_argument(
            "--actor",
            required=name
            in ("validate", "publish", "rollback", "withdraw", "retract-work"),
        )

    package = subparsers.add_parser("package", help="export package operations")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    validate_package = package_sub.add_parser("validate", help="validate a package directory")
    validate_package.add_argument("path", type=Path)

    legacy = subparsers.add_parser("legacy", help="legacy migration importers")
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    classify = legacy_sub.add_parser("classify", help="classify a legacy asset inventory A-E")
    classify.add_argument("inventory", type=Path, help="JSONL inventory of legacy artefacts")
    reacquire = legacy_sub.add_parser(
        "re-acquisition-list", help="what must be fetched again, and at what cost"
    )
    reacquire.add_argument("inventory", type=Path)

    demo = subparsers.add_parser(
        "demo", help="run the deterministic in-process stack end to end"
    )
    demo.add_argument("--question", default="What lipid yield was reported?")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings, provenance = load_settings()

    match args.command:
        case "config":
            if args.config_command == "connectors":
                return _config_connectors(settings)
            return _config_print(settings, provenance, as_json=args.json)
        case "db":
            return asyncio.run(_db(args, settings))
        case "storage":
            return asyncio.run(_storage(args, settings))
        case "backup":
            return asyncio.run(_backup(args, settings))
        case "task":
            return asyncio.run(_task(args, settings))
        case "worker":
            return asyncio.run(_worker(args, settings))
        case "release":
            return asyncio.run(_release(args, settings))
        case "package":
            return _package_validate(args.path)
        case "legacy":
            return asyncio.run(_legacy(args, settings))
        case "demo":
            return asyncio.run(_demo(args))
    return 2  # pragma: no cover - argparse rejects anything else


def _config_print(
    settings: Settings, provenance: ConfigProvenance, *, as_json: bool
) -> int:
    """Print effective configuration, redacted, with the source of each key."""
    redacted = settings.redacted()
    sources = provenance.as_dict()
    blockers = settings.production_blockers()
    if as_json:
        print(
            json.dumps(
                {
                    "environment": settings.env.value,
                    "config": redacted,
                    "sources": sources,
                    "production_blockers": list(blockers),
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    else:
        print(f"environment: {settings.env.value}")
        width = max(len(key) for key in redacted)
        for key in sorted(redacted):
            source = sources.get(key, "compiled default")
            print(f"  {key:<{width}}  {redacted[key]!r:<40}  [{source}]")
        if blockers:
            print("\nproduction blockers:")
            for blocker in blockers:
                print(f"  - {blocker}")
    return 1 if blockers else 0


def _config_connectors(settings: Settings) -> int:
    """Print the registered connectors, credentials as a boolean.

    An operator diagnosing a failed discovery run needs to know that `pubmed` is
    registered and keyless — the first question after `UnknownConnector` — and this
    output is safe to paste into a support bundle because it holds no credential.
    """
    from lab_assistant.adapters.connectors.registry import (
        live_connector_registry,
        registry_summary,
    )

    summary = registry_summary(live_connector_registry(settings))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


async def _db(args: argparse.Namespace, settings: Settings) -> int:
    from lab_assistant.adapters.db.engine import create_schema, make_engine
    from lab_assistant.adapters.db.tables import METADATA

    if args.db_command in ("create-admin", "verify-audit", "verify-restore", "revoke-sessions"):
        return await _db_service_command(args, settings)

    engine = make_engine(settings)
    try:
        if args.db_command == "create":
            await create_schema(engine)
            print(f"created {len(METADATA.tables)} tables in {settings.sync_database_url}")
            return 0
        # `check`: every table the application declares must exist.
        from sqlalchemy import inspect

        async with engine.connect() as connection:
            existing = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        missing = sorted(set(METADATA.tables) - existing)
        if missing:
            print("missing tables:", ", ".join(missing), file=sys.stderr)
            return 1
        print(f"schema matches: {len(METADATA.tables)} tables present")
        return 0
    finally:
        await engine.dispose()


async def _db_service_command(args: argparse.Namespace, settings: Settings) -> int:
    """Operator commands that go through the application services, not raw SQL.

    Every one of these is referenced by a runbook. They exist as commands rather
    than as psql recipes because a hand-written ``UPDATE`` leaves an unexplained
    state and an audit trail that still verifies while being incomplete.
    """
    import os
    from getpass import getpass

    from lab_assistant.application.accounts import AccountService
    from lab_assistant_api.deps import build_container

    container = build_container(settings)
    try:
        accounts = AccountService(container.context)

        if args.db_command == "create-admin":
            # The password never comes from argv: it would be visible in `ps` and in
            # the operator's shell history.
            password = os.environ.get("LAB_ADMIN_PASSWORD") or getpass("admin password: ")
            if len(password) < 12:
                print("password must be at least 12 characters", file=sys.stderr)
                return 1
            user = await accounts.create_user(email=args.email, password=password)
            workspace, membership = await accounts.create_workspace(
                slug=args.workspace.lower(),
                display_name=args.display_name or args.workspace,
                owner_user_id=user.id,
            )
            print(
                json.dumps(
                    {
                        "user_id": str(user.id),
                        "workspace_id": str(workspace.id),
                        "workspace_slug": workspace.slug,
                        "role": membership.role.value,
                    },
                    indent=2,
                )
            )
            return 0

        if args.db_command == "revoke-sessions":
            user = await accounts.revoke_sessions(user_id=UUID(args.user))
            print(f"sessions revoked for {user.id}; auth epoch is now {user.auth_epoch}")
            return 0

        workspace_id = await _workspace_id(container, args.workspace)
        if workspace_id is None:
            print(f"workspace {args.workspace!r} not found", file=sys.stderr)
            return 1
        if args.db_command == "verify-audit":
            ok, problems = await _verify_audit(container, workspace_id)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("audit chain verifies" if ok else "audit chain FAILED", file=sys.stderr)
            return 0 if ok else 1
        return await _verify_restore(container, workspace_id)
    finally:
        for hook in container.shutdown:
            await hook()


async def _workspace_id(container: Any, slug: str) -> UUID | None:
    async with container.context.uow_factory() as uow:
        workspace = await uow.workspaces.get_by_slug(slug.lower())
    return workspace.id if workspace else None


async def _verify_audit(container: Any, workspace_id: UUID) -> tuple[bool, tuple[str, ...]]:
    from lab_assistant.provenance.audit import verify_chain

    async with container.context.uow_factory() as uow:
        events = await uow.audit.list_events(workspace_id, limit=1_000_000)
    return verify_chain(events)


async def _verify_restore(container: Any, workspace_id: UUID) -> int:
    """Confirm a restored deployment is coherent before it serves anything.

    A restore fails in more ways than one, and each needs its own answer:

    - the audit chain can be truncated;
    - a row can point at an object nothing registered, which is what every
      installation that predates the object registry looks like;
    - a registered object's bytes can be absent, denied, cold or corrupt — four
      different next moves, and reporting them as one loses the distinction;
    - a stored manifest can disagree with the digest the release records, or be
      unparseable, which is itself the finding rather than a traceback;
    - a release can have every byte it needs and no search projection, which
      makes it unservable for a different reason;
    - a manifest can name claim revisions or a scope version the database no
      longer holds, which is a release that cannot answer however intact its
      bytes are;
    - an alias can point at a release that is not servable.

    Everything pages with a keyset cursor. The previous implementation read a
    thousand releases and declared the restore verified, which is the failure the
    corpus was already large enough to hit.
    """
    from lab_assistant.application.pagination import iter_pages
    from lab_assistant.application.storage_inventory import (
        PayloadCheck,
        StorageInventoryService,
    )
    from lab_assistant.contracts.release import ReleaseManifest

    problems: list[str] = []
    releases_checked = manifests_checked = aliases_checked = projections_checked = 0
    claims_checked = claims_missing = 0

    chain_ok, chain_problems = await _verify_audit(container, workspace_id)
    problems.extend(f"audit: {problem}" for problem in chain_problems)

    context = container.context
    inventory = await StorageInventoryService(context).sweep(
        workspace_id, payload_check=PayloadCheck.DIGEST
    )
    problems.extend(f"storage: {finding.detail}" for finding in inventory.blocking)

    async with context.uow_factory() as uow:
        async for release in iter_pages(
            lambda after, size: uow.releases.list_all_releases(
                workspace_id, limit=size, after_id=after
            )
        ):
            if not release.status.is_servable:
                continue
            releases_checked += 1
            if release.release_manifest_object_id is None:
                problems.append(f"release {release.id}: no stored manifest object")
                continue
            try:
                payload = await context.object_store.get(release.release_manifest_object_id)
            except Exception as exc:  # a missing or corrupt object is the finding
                problems.append(f"release {release.id}: manifest object unreadable ({exc})")
                continue
            try:
                manifest = ReleaseManifest.model_validate(json.loads(payload))
            except Exception as exc:
                # A manifest that will not parse used to raise out of the whole
                # check, so one bad release hid every finding after it.
                problems.append(f"release {release.id}: manifest does not parse ({exc})")
                continue
            manifests_checked += 1
            # A servable release that declared a projection and has none cannot
            # answer a question. The bytes can all be present and the release
            # still be unservable, so this is its own check.
            if release.projection_ready_profiles and context.search_index is not None:
                chunks = await context.search_index.count_chunks(workspace_id, release.id)
                if chunks == 0:
                    problems.append(
                        f"release {release.id}: declares projections for "
                        f"{', '.join(release.projection_ready_profiles)} and has no chunks"
                    )
                else:
                    projections_checked += 1
            if release.manifest_sha256 and manifest.manifest_sha256 != release.manifest_sha256:
                problems.append(
                    f"release {release.id}: stored manifest digests to "
                    f"{manifest.manifest_sha256[:12]}…, the release records "
                    f"{release.manifest_sha256[:12]}…"
                )
            for scope_version_id in manifest.scope_version_ids:
                if await uow.configs.get_scope_version(workspace_id, scope_version_id) is None:
                    problems.append(
                        f"release {release.id}: scope version {scope_version_id} is absent"
                    )

            for record in manifest.items:
                # The claim revisions this release serves. A manifest naming a
                # revision the database no longer has is a release that cannot
                # answer about that claim, however intact its bytes are.
                for revision_id in record.claim_revision_ids:
                    if await uow.claims.get_revision(workspace_id, revision_id) is None:
                        problems.append(
                            f"release {release.id}: claim revision {revision_id} "
                            f"for work {record.work_id} is absent"
                        )
                        claims_missing += 1
                    else:
                        claims_checked += 1
                if record.rendition_digest is None:
                    continue
                stored = await context.object_store.get_by_digest(
                    record.rendition_digest, workspace_id=workspace_id
                )
                if stored is None:
                    problems.append(
                        f"release {release.id}: object {record.rendition_digest[:12]}… "
                        f"for work {record.work_id} is not in the store"
                    )

        for alias in await uow.releases.list_aliases(workspace_id):
            aliases_checked += 1
            if alias.release_id is None:
                problems.append(f"alias {alias.alias}: points at nothing")
                continue
            target = await uow.releases.get(workspace_id, alias.release_id)
            if target is None:
                problems.append(f"alias {alias.alias}: release {alias.release_id} is absent")
            elif not target.status.is_servable:
                problems.append(
                    f"alias {alias.alias}: release {alias.release_id} is "
                    f"{target.status.value}, which is not servable"
                )

    advisory = [finding.as_dict() for finding in inventory.findings if not finding.blocks_serving]
    print(
        json.dumps(
            {
                "audit_chain": "ok" if chain_ok else "failed",
                "releases_checked": releases_checked,
                "manifests_checked": manifests_checked,
                "projections_checked": projections_checked,
                "claim_revisions_checked": claims_checked,
                "claim_revisions_missing": claims_missing,
                "aliases_checked": aliases_checked,
                "storage": inventory.as_dict(),
                "advisory": advisory,
                "problems": problems,
            },
            indent=2,
        )
    )
    if problems:
        print(
            f"{len(problems)} problem(s): do not serve this restore "
            "until each one is explained",
            file=sys.stderr,
        )
        return 1
    print("restore verified", file=sys.stderr)
    return 0


async def _storage(
    args: argparse.Namespace, settings: Settings, *, container: Any | None = None
) -> int:
    """Sweep the join between the database and the object store, or repair it.

    ``container`` is injectable so a test drives the same code an operator does.
    """
    from lab_assistant.application.storage_inventory import (
        PayloadCheck,
        StorageInventoryService,
    )

    owned = container is None
    if container is None:
        from lab_assistant_api.deps import build_container

        container = build_container(settings)
    try:
        if args.storage_command == "migrate":
            return await _storage_migrate(args, settings, container)

        workspace_id = await _workspace_id(container, args.workspace)
        if workspace_id is None:
            print(f"workspace {args.workspace!r} not found", file=sys.stderr)
            return 1
        service = StorageInventoryService(container.context)

        if args.storage_command == "scan":
            report = await service.sweep(
                workspace_id,
                payload_check=PayloadCheck(args.verify),
                report_orphans=not args.skip_orphans,
            )
            print(json.dumps(report.as_dict(), indent=2))
            if report.blocking:
                print(
                    f"{len(report.blocking)} blocking finding(s): do not serve this "
                    "installation until each one is explained",
                    file=sys.stderr,
                )
                return 1
            if report.findings:
                print(
                    f"{len(report.findings)} finding(s), none blocking serving",
                    file=sys.stderr,
                )
            else:
                print("storage and database agree", file=sys.stderr)
            return 0

        report = await service.backfill(workspace_id, dry_run=not args.apply)
        print(json.dumps(report.as_dict(), indent=2))
        if report.unrecoverable:
            print(
                f"{len(report.unrecoverable)} reference(s) the store cannot account for "
                "either: those payloads are gone, and no registry row can be invented "
                "for them",
                file=sys.stderr,
            )
            return 1
        verb = "would register" if report.dry_run else "registered"
        print(f"{verb} {report.registered} object(s)", file=sys.stderr)
        return 0
    finally:
        if owned:
            for hook in container.shutdown:
                await hook()



def _destination_store(settings: Settings):
    """Build the migration destination from its own environment namespace.

    ``LAB_MIGRATE_*`` mirrors ``LAB_OBJECT_STORE_*``. A separate namespace
    because both stores have to be described at once, and credentials stay in
    the environment rather than on a command line that lands in shell history
    and process listings.
    """
    import os

    kind = os.environ.get("LAB_MIGRATE_OBJECT_STORE_KIND")
    if not kind:
        raise LabAssistantError(
            "no migration destination configured",
            context={
                "set": "LAB_MIGRATE_OBJECT_STORE_KIND=filesystem|s3",
                "then": "LAB_MIGRATE_OBJECT_STORE_ROOT, or the LAB_MIGRATE_OBJECT_STORE_* "
                "bucket, region, endpoint and credentials",
            },
        ).with_class(ErrorClass.INVALID_CONTENT)

    if kind == "filesystem":
        root = os.environ.get("LAB_MIGRATE_OBJECT_STORE_ROOT")
        if not root:
            raise LabAssistantError(
                "LAB_MIGRATE_OBJECT_STORE_ROOT is required for a filesystem destination"
            ).with_class(ErrorClass.INVALID_CONTENT)
        from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore

        return FilesystemObjectStore(Path(root))

    if kind == "s3":
        from lab_assistant.adapters.object_store.s3 import S3ObjectStore, S3StoreConfig

        return S3ObjectStore(
            S3StoreConfig(
                bucket=os.environ.get("LAB_MIGRATE_OBJECT_STORE_BUCKET"),
                region=os.environ.get("LAB_MIGRATE_OBJECT_STORE_REGION"),
                endpoint=os.environ.get("LAB_MIGRATE_OBJECT_STORE_ENDPOINT"),
                access_key=os.environ.get("LAB_MIGRATE_OBJECT_STORE_ACCESS_KEY"),
                secret_key=os.environ.get("LAB_MIGRATE_OBJECT_STORE_SECRET_KEY"),
                session_token=os.environ.get("LAB_MIGRATE_OBJECT_STORE_SESSION_TOKEN"),
                prefix=os.environ.get("LAB_MIGRATE_OBJECT_STORE_PREFIX", ""),
                multipart_threshold=settings.object_store_multipart_threshold_bytes,
            )
        )

    raise LabAssistantError(
        f"unknown LAB_MIGRATE_OBJECT_STORE_KIND {kind!r}; expected 'filesystem' or 's3'"
    ).with_class(ErrorClass.INVALID_CONTENT)


async def _storage_migrate(args: argparse.Namespace, settings: Settings, container: Any) -> int:
    """Inventory both stores, then copy — installation-wide, not per workspace."""
    from lab_assistant.application.storage_migration import (
        MigrationJournal,
        StorageMigrationService,
        VerifyLevel,
    )

    destination = _destination_store(settings)
    run_id = args.run_id or uuid4().hex
    journal_path = args.journal or (
        settings.object_store_root.parent / "migrations" / f"{run_id}.jsonl"
    )
    journal = MigrationJournal(Path(journal_path))
    service = StorageMigrationService(container.context, destination=destination)

    report = await service.migrate(
        journal=journal if args.apply else None,
        dry_run=not args.apply,
        verify=VerifyLevel(args.verify),
        include_source_only=not args.skip_source_only,
        run_id=run_id,
    )
    payload = report.as_dict()
    payload["journal"] = str(journal.path)
    print(json.dumps(payload, indent=2))

    close = getattr(destination, "aclose", None)
    if close is not None:
        await close()

    if report.problems:
        print(
            f"{len(report.problems)} object(s) did not migrate: the old store stays "
            "primary until each one is explained",
            file=sys.stderr,
        )
        return 1
    if report.dry_run:
        print(
            f"would copy {report.copied} object(s), {report.bytes_copied} bytes; "
            f"re-run with --apply (resume id {report.run_id})",
            file=sys.stderr,
        )
        return 0
    print(
        f"copied {report.copied} object(s), verified {report.verified}; "
        "the old store is still primary until you change the configuration",
        file=sys.stderr,
    )
    return 0


async def _backup(
    args: argparse.Namespace, settings: Settings, *, container: Any | None = None
) -> int:
    """Take, check, or put back a private recovery backup.

    ``container`` is injectable so a test drives the same code an operator does,
    rather than a parallel implementation that could disagree with it.
    """
    from lab_assistant.adapters.db.migrations import migration_order
    from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
    from lab_assistant.application.backup import BackupService, installation_identity
    from lab_assistant.application.storage_migration import VerifyLevel
    from lab_assistant.domain.recovery import RestoreIntent

    owned = container is None
    if container is None:
        from lab_assistant_api.deps import build_container

        container = build_container(settings)
    try:
        snapshot = getattr(container, "database_snapshot", None)
        if snapshot is None:
            snapshot = _database_snapshot(container, settings)
        installation_id, identity_source = installation_identity(settings)
        service = BackupService(
            container.context,
            snapshot=snapshot,
            installation_id=installation_id,
            store_factory=FilesystemObjectStore,
            writer_epoch=settings.writer_epoch,
            identity_source=identity_source,
        )

        if args.backup_command == "create":
            report = await service.create(
                args.directory,
                include_objects=not args.skip_objects,
                dry_run=args.dry_run,
                verify=VerifyLevel(args.verify),
            )
            print(json.dumps(report.as_dict(), indent=2, default=str))
            if report.problems:
                for problem in report.problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 1
            if report.dry_run:
                print(
                    "dry run: nothing was written. Re-run without --dry-run to take it.",
                    file=sys.stderr,
                )
                return 0
            manifest = report.manifest
            assert manifest is not None  # create always builds one when it writes
            if manifest.incomplete:
                for item in manifest.incomplete:
                    print(f"  - incomplete: {item}", file=sys.stderr)
                print(
                    "this backup records a hole in itself; a known hole is not a recovery point",
                    file=sys.stderr,
                )
                return 1
            print(
                f"backup {manifest.backup_id} written to {report.directory}. "
                "Put it in encrypted off-site storage: it holds password hashes and "
                "unreviewed evidence.",
                file=sys.stderr,
            )
            return 0

        if args.backup_command == "verify":
            verification = await service.verify(args.directory, check_objects=not args.skip_objects)
            print(json.dumps(verification.as_dict(), indent=2, default=str))
            if not verification.ok:
                for problem in verification.problems:
                    print(f"  - {problem}", file=sys.stderr)
                print(
                    f"{len(verification.problems)} problem(s): this backup is not a "
                    "recovery point until each one is explained",
                    file=sys.stderr,
                )
                return 1
            print(
                f"verified {verification.components_checked} component(s) and "
                f"{verification.objects_checked} object(s)",
                file=sys.stderr,
            )
            return 0

        report = await service.restore(
            args.directory,
            intent=RestoreIntent(args.intent),
            apply=args.apply,
            origin_is_fenced=args.origin_is_fenced,
            migration_order=migration_order(),
            clean=args.clean,
            verify=VerifyLevel(args.verify),
        )
        print(json.dumps(report.as_dict(), indent=2, default=str))
        for step in report.next_steps:
            print(f"  next: {step}", file=sys.stderr)
        if report.problems:
            for problem in report.problems:
                print(f"  - {problem}", file=sys.stderr)
            print(
                f"{len(report.problems)} blocking finding(s): this restore is refused",
                file=sys.stderr,
            )
            return 1
        if not report.applied:
            print("preflight only: nothing was changed. Re-run with --apply.", file=sys.stderr)
        return 0
    finally:
        if owned:
            for hook in container.shutdown:
                await hook()


def _database_snapshot(container: Any, settings: Settings):
    """The dump/restore adapter over the container's own engine.

    Built here rather than in the container because nothing else needs it: an
    API process that held a `pg_dump` runner would be carrying a privilege it
    never uses.
    """
    from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot

    engine = getattr(container, "engine", None)
    if engine is None:
        from lab_assistant.adapters.db.engine import make_engine

        engine = make_engine(settings)
    return PostgresDatabaseSnapshot(engine, database_url=settings.database_url)


async def _task(
    args: argparse.Namespace, settings: Settings, *, container: Any | None = None
) -> int:
    """Inspect and steer one task.

    ``container`` is injectable so a test drives the same code an operator does,
    against the in-memory adapters, rather than a parallel implementation that
    could disagree with the real one.
    """
    owned = container is None
    if container is None:
        from lab_assistant_api.deps import build_container

        container = build_container(settings)
    try:
        workspace_id = await _workspace_id(container, args.workspace)
        if workspace_id is None:
            print(f"workspace {args.workspace!r} not found", file=sys.stderr)
            return 1

        if args.task_command == "queue":
            snapshot = await container.orchestrator.queue_snapshot(workspace_id=workspace_id)
            print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
            return 0

        task_id = UUID(args.task)
        if args.task_command == "retry":
            task = await container.orchestrator.retry_dead_letter(
                workspace_id=workspace_id, task_id=task_id
            )
            print(f"task {task.id} re-queued as {task.state.value} (attempt {task.attempt})")
            return 0

        async with container.context.uow_factory() as uow:
            task = await uow.tasks.get(workspace_id, task_id)
            if task is None:
                print(f"task {task_id} not found", file=sys.stderr)
                return 1
            if args.task_command == "cancel":
                if task.state.is_terminal:
                    print(
                        f"task {task_id} is already {task.state.value}; nothing to cancel",
                        file=sys.stderr,
                    )
                    return 1
                task.cancel(now=container.context.now())
                task.error_detail = args.reason
                await uow.tasks.update(task)
                await uow.commit()
                print(f"task {task_id} cancelled ({args.reason})")
                return 0
            print(
                json.dumps(
                    {
                        "id": str(task.id),
                        "stage": task.stage.value,
                        "state": task.state.value,
                        "attempt": task.attempt,
                        "max_attempts": task.max_attempts,
                        "lease_owner": task.lease_owner,
                        "lease_expires_at": task.lease_expires_at,
                        "not_before": task.not_before,
                        "depends_on": [str(item) for item in task.depends_on],
                        "error_class": task.error_class.value if task.error_class else None,
                        "error_detail": task.error_detail,
                        "counts_as_failure": task.state.counts_as_failure,
                        "input_fingerprint": task.input_fingerprint,
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0
    finally:
        if owned:
            for hook in container.shutdown:
                await hook()


async def _worker(args: argparse.Namespace, settings: Settings) -> int:
    import socket

    from lab_assistant_api.deps import build_container  # composition root reuse

    owner = args.owner or f"{socket.gethostname()}/{args.worker_class}"
    container = build_container(settings)
    orchestrator = container.orchestrator
    try:
        if args.once:
            result = await orchestrator.run_once(owner=owner, worker_class=args.worker_class)
            print(json.dumps(result.__dict__))
            return 0
        print(f"worker {owner} started (stages: {args.worker_class})", file=sys.stderr)
        while True:
            result = await orchestrator.run_once(owner=owner, worker_class=args.worker_class)
            if result.idle:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        print("worker stopping", file=sys.stderr)
        return 0
    finally:
        for hook in container.shutdown:
            await hook()


async def _release(args: argparse.Namespace, settings: Settings) -> int:
    from lab_assistant.application.context import resolve_actor
    from lab_assistant.application.releases import ReleaseService
    from lab_assistant_api.deps import build_container

    container = build_container(settings)
    try:
        async with container.context.uow_factory() as uow:
            workspace = await uow.workspaces.get_by_slug(args.workspace.lower())
        if workspace is None:
            print(f"workspace {args.workspace!r} not found", file=sys.stderr)
            return 1

        if args.release_command == "list":
            async with container.context.uow_factory() as uow:
                releases = await uow.releases.list_releases(workspace.id)
                aliases = await uow.releases.list_aliases(workspace.id)
            for release in releases:
                print(
                    f"{release.id}  {release.name}@{release.semantic_version}  "
                    f"{release.status.value:<11}  manifest={release.manifest_sha256}"
                )
            for alias in aliases:
                print(f"alias {alias.alias} -> {alias.release_id}")
            return 0

        if args.release_command == "references":
            # Answers "may I delete this object" before a disk-pressure decision:
            # deleting bytes a published release cites breaks every citation into it.
            return await _release_references(container, workspace.id, digest=args.digest)

        actor = await resolve_actor(
            container.context, user_id=UUID(args.actor), workspace_id=workspace.id
        )
        service = ReleaseService(container.context)
        if args.release_command == "validate":
            report = await service.validate(actor, release_id=UUID(args.release_id))
            print(json.dumps({"passed": report.passed, "gates": report.gates,
                              "failures": list(report.failures)}, indent=2))
            return 0 if report.passed else 1
        if args.release_command == "publish":
            alias = await service.publish(
                actor, release_id=UUID(args.release_id), alias=args.alias
            )
            print(f"alias {alias.alias} -> {alias.release_id}")
            return 0
        if args.release_command == "withdraw":
            release = await service.withdraw(actor, release_id=UUID(args.release_id))
            print(f"release {release.id} is now {release.status.value}")
            return 0
        if args.release_command == "retract-work":
            from lab_assistant.application.refresh import RefreshService

            plan = await RefreshService(container.context).record_retraction(
                actor, work_id=UUID(args.work_id), reason_code=args.reason
            )
            print(json.dumps(plan.as_dict(), indent=2, sort_keys=True, default=str))
            return 0
        alias = await service.rollback(actor, alias=args.alias)
        print(f"alias {alias.alias} rolled back to {alias.release_id}")
        return 0
    finally:
        for hook in container.shutdown:
            await hook()


async def _release_references(container: Any, workspace_id: UUID, *, digest: str) -> int:
    """Which releases cite a digest, and whether any of them is still served."""
    async with container.context.uow_factory() as uow:
        aliases = {
            alias.release_id: alias.alias
            for alias in await uow.releases.list_aliases(workspace_id)
            if alias.release_id is not None
        }
        citing: list[dict[str, Any]] = []
        for release in await uow.releases.list_releases(workspace_id, limit=1_000):
            items = await uow.releases.list_items(workspace_id, release.id)
            works = [
                str(item.record.work_id)
                for item in items
                if item.record.rendition_digest == digest
            ]
            if works:
                citing.append(
                    {
                        "release_id": str(release.id),
                        "status": release.status.value,
                        "served_as": aliases.get(release.id),
                        "work_ids": sorted(works),
                    }
                )
        asset = await uow.assets.find_asset_by_digest(workspace_id, digest)

    safe = not any(
        entry["served_as"] is not None or entry["status"] in ("published", "superseded")
        for entry in citing
    )
    print(
        json.dumps(
            {
                "digest": digest,
                "asset_id": str(asset.id) if asset else None,
                "citing_releases": citing,
                "safe_to_delete": safe,
            },
            indent=2,
        )
    )
    # Non-zero when something still needs it: a script can gate a deletion on this.
    return 0 if safe else 1


def _package_validate(path: Path) -> int:
    """Validate an export package on disk, offline."""
    from lab_assistant.application.export import ExportPackage, validate_package
    from lab_assistant.contracts.release import ExportPackageManifest

    if not path.is_dir():
        print(f"{path} is not a directory", file=sys.stderr)
        return 1
    files = {
        str(item.relative_to(path)): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }
    manifest_path = "metadata/release.json"
    if manifest_path not in files:
        print(f"missing {manifest_path}", file=sys.stderr)
        return 1
    payload_digests: dict[str, str] = {}
    for line in files.get("manifest-sha256.txt", b"").decode().splitlines():
        digest, _, name = line.partition("  ")
        if name:
            payload_digests[name] = digest
    tag_digests: dict[str, str] = {}
    for line in files.get("tagmanifest-sha256.txt", b"").decode().splitlines():
        digest, _, name = line.partition("  ")
        if name:
            tag_digests[name] = digest
    release = json.loads(files[manifest_path])
    manifest = ExportPackageManifest(
        release_id=UUID(release["release_id"]),
        workspace_id=UUID(release.get("workspace_id", str(UUID(int=0)))),
        payload_digests=payload_digests,
        tag_digests=tag_digests,
    )
    valid, problems = validate_package(ExportPackage(files=files, manifest=manifest))
    if valid:
        print(f"package at {path} is valid ({len(payload_digests)} payload files)")
        return 0
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


async def _legacy(args: argparse.Namespace, settings: Settings) -> int:
    from lab_assistant.migration.legacy_import import LegacyImporter
    from lab_assistant_api.deps import build_container

    container = build_container(settings)
    try:
        importer = LegacyImporter(container.context)
        records = LegacyImporter.load_jsonl(args.inventory)
        if args.legacy_command == "re-acquisition-list":
            print(json.dumps(importer.re_acquisition_list(records), indent=2, sort_keys=True))
            return 0
        # `classify` runs without a workspace actor: it only reads and reports.
        from collections import Counter

        from lab_assistant.migration.legacy_import import classify_asset

        counts: Counter[str] = Counter()
        rows: list[dict[str, Any]] = []
        for record in records:
            asset_class, reason = classify_asset(record)
            counts[asset_class.value] += 1
            rows.append(
                {
                    "id": record.get("path") or record.get("id"),
                    "class": asset_class.value,
                    "reason": reason,
                }
            )
        print(json.dumps({"counts": dict(sorted(counts.items())), "rows": rows}, indent=2))
        return 0
    finally:
        for hook in container.shutdown:
            await hook()


async def _demo(args: argparse.Namespace) -> int:
    """Publish a small corpus and answer a question against it, in process.

    No network, no database, no model provider: in-memory adapters, a fixed clock,
    and the fake provider. It runs the same services the API runs, so what it
    proves is real — a citation minted here resolves through the same lineage code
    a deployment uses.
    """
    from lab_assistant.application.chat import ChatService
    from lab_assistant.contracts.policy import (
        Effect,
        PolicyAction,
        ResourceType,
        SubjectType,
    )
    from lab_assistant.domain.workspace import Role
    from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

    harness = build_harness()
    user = await harness.accounts.create_user(
        email="demo@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="demo", display_name="Demo lab", owner_user_id=user.id
    )
    admin = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="demo_open_corpus",
        )
        for action in (
            PolicyAction.QUERY,
            PolicyAction.VIEW_CONTENT,
            PolicyAction.INDEX,
            PolicyAction.DISPLAY_EXCERPT,
            PolicyAction.DISPLAY_FULL_CONTENT,
        )
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )

    corpus = await publish_demo_corpus(harness, actor)
    chat = ChatService(harness.context)
    session = await chat.create_session(actor)
    outcome = await chat.ask(actor, session_id=session.id, question=args.question)

    citations = outcome.message.citations
    print(outcome.text)
    print()
    resolved = 0
    for citation in citations:
        evidence = await chat.resolve_citation(
            actor, message_id=outcome.message.id, handle=citation.handle
        )
        if not evidence.get("resolved"):
            print(f"  [{citation.handle}] unresolved: {evidence.get('reason')}")
            continue
        resolved += 1
        print(f"  [{citation.handle}] {_evidence_preview(evidence)}")
    print(
        json.dumps(
            {
                "workspace": workspace.slug,
                "release_id": str(corpus.release_id),
                "alias_current": str(corpus.served_release_id),
                "citations": len(citations),
                "citations_resolved": resolved,
                "unsupported_handles": list(outcome.unsupported_handles),
                "audit_events": len(harness.db.audit_events),
            },
            indent=2,
        )
    )
    # An answer nobody can trace is the failure this system exists to prevent, so
    # the demo fails rather than printing a citation-free wall of text.
    if citations and resolved == len(citations):
        return 0
    print("demo produced no resolvable citation", file=sys.stderr)
    return 1


def _evidence_preview(evidence: dict[str, Any], *, width: int = 110) -> str:
    """The first line of what a citation opened, span or node."""
    span = evidence.get("span")
    if isinstance(span, dict) and span.get("quote_preview"):
        text = str(span["quote_preview"])
    else:
        nodes = evidence.get("nodes") or []
        text = str(nodes[0].get("text", "")) if nodes else ""
    text = " ".join(text.split())
    return text[:width] + ("…" if len(text) > width else "")


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
