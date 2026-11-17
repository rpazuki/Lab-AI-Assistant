"""Whether this process should be sent traffic.

Three things were wrong with the check this replaces, and each of them fails in
the direction that looks fine.

**It answered 200 while reporting `degraded`.** The body said the object store
was unreachable and the status line said OK, so every generic health tool — a
load balancer, a Compose `healthcheck`, a Kubernetes probe, anything that reads
the status code and not the JSON — routed traffic to a process that could not
serve a citation. The deployment helper happened to parse the body, which is
why this survived: the one consumer that read it carefully made the broken
contract invisible to everyone else.

**It wrote to the object store to prove the object store worked.** On the
filesystem profile the probe stored a payload on every call. Readiness runs on a
timer, so that is a write amplified by however often the probe fires, into the
store that holds the evidence. A probe must not change what it observes.

**It never asked whether the schema matched.** An API container that starts
before its migrations have run reaches the database, answers every readiness
check, and then fails on the first real query. That is the single most common
way a deploy goes wrong, and it was the one thing not checked.

What a check may do here: read. Nothing in this module writes, and a check that
cannot answer says so rather than guessing — an unknown is reported as unknown
and does not block serving, because a probe that fails closed on its own
inability to measure takes a working deployment down.

Liveness and readiness are different questions and live apart. `/health` asks
whether the process is running and must not touch a dependency: a database
blip that made every container report unhealthy would have an orchestrator
restart all of them, which is the opposite of what the situation needs.
`/ready` asks whether this process should receive a request, and that one does
depend on the things a request needs.

The checks are **role-specific**, which was the half of F15 that had to wait for
the installation role to exist:

- a **reader** proves the store by fetching a manifest it is expected to serve.
  The bucket-wide LIST a publisher uses is exactly the permission a narrowly
  scoped reader credential is not given, so a reader probed that way reports
  itself unfit while being perfectly able to serve every object it needs;
- a **reader** with nothing servable is not ready. An import that has not landed
  leaves an installation that answers every question with nothing;
- a **publisher** with no release yet is a normal, working installation that
  stewards are still building a corpus on, so the same condition is advisory
  there. The asymmetry is the point: the two roles fail differently.

Writing remains outside this module. A publisher's ability to write is not
probed on a timer, because a probe that writes on a timer is the defect this
module was opened to remove; it belongs to an operator command run deliberately.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from lab_assistant.application.context import ServiceContext

__all__ = [
    "ReadinessCheck",
    "ReadinessReport",
    "ReadinessService",
    "ReadinessState",
    "SchemaRevisionReader",
]

#: How many workspaces a probe will look through for something servable. A
#: readiness check runs on a timer, so its cost must not grow with the corpus.
_WORKSPACE_PROBE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class _Servable:
    """The release this installation is currently serving, if any."""

    workspace_id: UUID
    release_id: UUID
    alias: str
    manifest_object_id: UUID | None


#: Returns the migration revision the database is stamped at, or ``None`` when
#: there is no such record. A narrow seam on purpose: readiness needs one string
#: and must not drag a dump-and-restore adapter — and the privileges that come
#: with it — into a web process.
SchemaRevisionReader = Callable[[], Awaitable[str | None]]


class ReadinessState(StrEnum):
    READY = "ready"
    #: Running, and must not be sent a request. Distinct from a dead process:
    #: an orchestrator should stop routing to this one, not restart it, because
    #: restarting does not fix a database that is down.
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ok: bool
    detail: str | None = None
    #: False for a check that could not be performed. An unknown is not a
    #: failure: a readiness probe that cannot measure something and therefore
    #: declares the process unfit takes down a deployment that was working.
    blocks_serving: bool = True

    @property
    def blocking_failure(self) -> bool:
        return not self.ok and self.blocks_serving

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "blocks_serving": self.blocks_serving,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(check.blocking_failure for check in self.checks)

    @property
    def state(self) -> ReadinessState:
        return ReadinessState.READY if self.ready else ReadinessState.NOT_READY

    @property
    def http_status(self) -> int:
        """200 when this process should be sent traffic, 503 when it should not.

        The status code is the contract. Anything that reads only the code must
        reach the same conclusion as something that parses the body, or the body
        is decoration.
        """
        return 200 if self.ready else 503

    def as_dict(self) -> dict[str, object]:
        return {
            # Kept as `ok` for a ready process: `scripts/lib/common.sh` and any
            # operator's saved one-liner match on it, and there is no reason to
            # break them to rename a string.
            "status": "ok" if self.ready else "not_ready",
            "state": self.state.value,
            "checks": [check.as_dict() for check in self.checks],
        }


class ReadinessService:
    """Reads the things a request needs, and reports whether they answered."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        schema_revision: SchemaRevisionReader | None = None,
        expected_revision: str | None = None,
    ) -> None:
        self._context = context
        self._schema_revision = schema_revision
        self._expected_revision = expected_revision

    async def check(self) -> ReadinessReport:
        servable = await self._servable_release()
        return ReadinessReport(
            checks=(
                await self._database(),
                await self._schema(),
                await self._object_store(servable),
                self._corpus(servable),
            )
        )

    async def _database(self) -> ReadinessCheck:
        try:
            async with self._context.uow_factory() as uow:
                await uow.users.list_all(limit=1)
        except Exception as exc:  # reported, never raised out of a probe
            return ReadinessCheck(
                name="database",
                ok=False,
                detail=f"the control plane did not answer ({type(exc).__name__})",
            )
        return ReadinessCheck(name="database", ok=True)

    async def _schema(self) -> ReadinessCheck:
        """Is the database at the revision this code expects?

        The common deploy failure: the API container starts, the migration has
        not run, and every query against a column added this release fails after
        the probe has already said the process is fit to serve.
        """
        if self._schema_revision is None or self._expected_revision is None:
            return ReadinessCheck(
                name="schema",
                ok=True,
                detail="not checked: no migration revision was supplied to this process",
                blocks_serving=False,
            )
        try:
            actual = await self._schema_revision()
        except Exception as exc:
            return ReadinessCheck(
                name="schema",
                ok=False,
                detail=f"could not read the migration revision ({type(exc).__name__})",
                blocks_serving=False,
            )
        if actual is None:
            return ReadinessCheck(
                name="schema",
                ok=True,
                detail=(
                    "the database carries no migration revision; it was built by "
                    "`lab db create` rather than by Alembic"
                ),
                blocks_serving=False,
            )
        if actual != self._expected_revision:
            return ReadinessCheck(
                name="schema",
                ok=False,
                detail=(
                    f"the database is at {actual} and this build expects "
                    f"{self._expected_revision}: run the migration before serving"
                ),
            )
        return ReadinessCheck(name="schema", ok=True, detail=actual)

    async def _object_store(self, servable: _Servable | None) -> ReadinessCheck:
        """Can the store be reached, without changing it — and as whom?

        A publisher asks the store for its cheapest reachability answer, which
        for a bucket is a LIST. A reader must not: a credential scoped to GET
        the objects it serves is a credential that cannot LIST the bucket, and
        probing it that way reports an installation unfit while every object it
        actually needs is readable. So a reader fetches a manifest it is
        expected to serve, which is the narrowest request that proves the thing
        in question.
        """
        store = self._context.object_store
        if not self._context.installation.is_publisher:
            return await self._reader_probe(store, servable)

        ping = getattr(store, "ping", None)
        if ping is None:
            return ReadinessCheck(
                name="object_store",
                ok=True,
                detail=f"not checked: {type(store).__name__} offers no read-only probe",
                blocks_serving=False,
            )
        try:
            await ping()
        except Exception as exc:
            return ReadinessCheck(
                name="object_store",
                ok=False,
                detail=f"the object store did not answer ({type(exc).__name__})",
            )
        return ReadinessCheck(name="object_store", ok=True)

    async def _reader_probe(self, store, servable: _Servable | None) -> ReadinessCheck:
        """A GET of one object this installation is expected to serve."""
        if servable is None or servable.manifest_object_id is None:
            return ReadinessCheck(
                name="object_store",
                ok=True,
                detail=(
                    "not checked: no servable release names a manifest object to fetch, "
                    "so there is no sentinel a reader credential is guaranteed to hold"
                ),
                blocks_serving=False,
            )
        try:
            found = await store.head(servable.manifest_object_id)
        except Exception as exc:
            return ReadinessCheck(
                name="object_store",
                ok=False,
                detail=(
                    f"cannot fetch the manifest of the release this installation serves "
                    f"({type(exc).__name__})"
                ),
            )
        if found is None:
            return ReadinessCheck(
                name="object_store",
                ok=False,
                detail=(
                    f"the store has no manifest object for release {servable.release_id}, "
                    "which this installation is serving"
                ),
            )
        return ReadinessCheck(
            name="object_store",
            ok=True,
            detail=f"fetched the manifest of release {servable.release_id}",
        )

    def _corpus(self, servable: _Servable | None) -> ReadinessCheck:
        """Is there anything to serve?

        Blocking for a reader and advisory for a publisher, because the same
        state means different things. A reader whose import has not landed
        answers every question with nothing and should not be in rotation; a
        publisher with no release yet is a working installation that stewards
        are still building a corpus on.
        """
        publisher = self._context.installation.is_publisher
        if servable is not None:
            return ReadinessCheck(
                name="corpus",
                ok=True,
                detail=f"serving release {servable.release_id} as {servable.alias!r}",
            )
        return ReadinessCheck(
            name="corpus",
            ok=publisher,
            detail=(
                "no alias points at a servable release"
                + (
                    "; a publisher with no release yet is still being set up"
                    if publisher
                    else ", so this installation has nothing to answer with"
                )
            ),
            blocks_serving=not publisher,
        )

    async def _servable_release(self) -> _Servable | None:
        """The first alias anywhere that points at a servable release.

        Readiness is not workspace-scoped, and in v1 a library maps to one
        workspace, so the first match is the corpus this installation serves.
        It stops at the first one: this runs on a timer, and a probe whose cost
        grows with the number of workspaces is a probe that gets turned off.
        """
        try:
            async with self._context.uow_factory() as uow:
                workspaces = await uow.workspaces.list_workspaces(limit=_WORKSPACE_PROBE_LIMIT)
                for workspace in workspaces:
                    for alias in await uow.releases.list_aliases(workspace.id):
                        if alias.release_id is None:
                            continue
                        release = await uow.releases.get(workspace.id, alias.release_id)
                        if release is None or not release.status.is_servable:
                            continue
                        return _Servable(
                            workspace_id=workspace.id,
                            release_id=release.id,
                            alias=alias.alias,
                            manifest_object_id=release.release_manifest_object_id,
                        )
        except Exception:
            # The database check reports this; it must not be reported twice,
            # and it must not mask itself as a storage failure.
            return None
        return None
