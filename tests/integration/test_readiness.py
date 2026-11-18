"""What the readiness probe answers, and what it refuses to answer.

The check this replaces failed in three ways that all looked like success: it
returned 200 while reporting a dependency down, it proved the object store
worked by writing to it, and it never asked whether the schema matched the
code. Each of those is a test here.

The fourth property is the one that is easy to lose later: a check that *cannot*
measure something must not report failure. A probe that fails closed on its own
blindness takes a working deployment out of service, which is worse than the
gap it was guarding.
"""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from lab_assistant.adapters.object_store.filesystem import (
    FilesystemObjectStore,
    InMemoryObjectStore,
)
from lab_assistant.application.readiness import (
    ReadinessReport,
    ReadinessService,
    ReadinessState,
)
from lab_assistant.testing import build_harness


def _by_name(report: ReadinessReport) -> dict[str, object]:
    return {check.name: check for check in report.checks}


@pytest.fixture
def harness():
    return build_harness()


class TestAHealthyProcess:
    async def test_it_is_ready_and_answers_200(self, harness) -> None:
        report = await ReadinessService(harness.context).check()
        assert report.ready
        assert report.state is ReadinessState.READY
        assert report.http_status == 200
        assert report.as_dict()["status"] == "ok"

    async def test_every_dependency_is_named(self, harness) -> None:
        """A probe that reports a single boolean cannot tell an operator which
        dependency to go and look at."""
        checks = _by_name(await ReadinessService(harness.context).check())
        assert set(checks) == {"database", "schema", "object_store", "corpus", "installation_role"}


class TestADependencyThatStoppedAnswering:
    async def test_an_unreachable_database_answers_503(self, harness) -> None:
        """The status code is the contract.

        This returned 200 with `"status": "degraded"`, so every load balancer
        and container healthcheck — anything reading the code rather than the
        JSON — kept sending requests to a process that could not serve them.
        """

        def broken():
            raise RuntimeError("connection refused")

        context = replace(harness.context, uow_factory=broken)
        report = await ReadinessService(context).check()

        assert not report.ready
        assert report.http_status == 503
        assert report.as_dict()["status"] == "not_ready"
        assert _by_name(report)["database"].ok is False

    async def test_an_unreachable_object_store_answers_503(self, harness) -> None:
        class Unreachable(InMemoryObjectStore):
            async def ping(self) -> None:
                raise OSError("the bucket did not answer")

        context = replace(harness.context, object_store=Unreachable())
        report = await ReadinessService(context).check()

        assert not report.ready
        assert report.http_status == 503
        assert _by_name(report)["object_store"].ok is False


class TestTheSchemaCheck:
    """An API container that starts before its migrations have run reaches the
    database, passes every other check, and fails on the first real query. It
    was the one thing the old probe did not look at."""

    async def test_a_matching_revision_is_ready(self, harness) -> None:
        async def revision() -> str:
            return "0001_initial"

        report = await ReadinessService(
            harness.context, schema_revision=revision, expected_revision="0001_initial"
        ).check()
        assert report.ready
        assert _by_name(report)["schema"].detail == "0001_initial"

    async def test_a_database_behind_the_code_refuses_traffic(self, harness) -> None:
        async def revision() -> str:
            return "0001_initial"

        report = await ReadinessService(
            harness.context, schema_revision=revision, expected_revision="0002_later"
        ).check()
        assert not report.ready
        assert report.http_status == 503
        assert "run the migration" in _by_name(report)["schema"].detail

    async def test_an_unstamped_database_is_reported_not_refused(self, harness) -> None:
        """`lab db create` builds a schema without Alembic. That is a real
        state — a scratch installation — not a reason to refuse traffic."""

        async def revision() -> None:
            return None

        report = await ReadinessService(
            harness.context, schema_revision=revision, expected_revision="0001_initial"
        ).check()
        assert report.ready
        schema = _by_name(report)["schema"]
        assert schema.blocks_serving is False
        assert "no migration revision" in schema.detail

    async def test_an_unreadable_revision_does_not_take_the_process_down(self, harness) -> None:
        """Failing closed on an inability to measure is how a probe turns a
        working deployment into an outage."""

        async def revision() -> str:
            raise RuntimeError("permission denied on alembic_version")

        report = await ReadinessService(
            harness.context, schema_revision=revision, expected_revision="0001_initial"
        ).check()
        assert report.ready
        assert _by_name(report)["schema"].ok is False
        assert _by_name(report)["schema"].blocks_serving is False

    async def test_a_process_told_nothing_does_not_guess(self, harness) -> None:
        report = await ReadinessService(harness.context).check()
        schema = _by_name(report)["schema"]
        assert schema.blocks_serving is False
        assert "not checked" in schema.detail


class TestTheProbeDoesNotMutate:
    async def test_it_writes_nothing_to_the_object_store(self, tmp_path) -> None:
        """The probe this replaces stored a payload every time it ran, into the
        store that holds the evidence."""
        store = FilesystemObjectStore(tmp_path / "objects")
        harness = build_harness(object_store=store)
        service = ReadinessService(harness.context)

        before = sorted(p.name for p in (tmp_path / "objects").rglob("*") if p.is_file())
        for _ in range(5):
            assert (await service.check()).ready
        after = sorted(p.name for p in (tmp_path / "objects").rglob("*") if p.is_file())
        assert after == before

    async def test_a_missing_store_root_is_a_failure_not_a_creation(self, tmp_path) -> None:
        """Readiness must not bring the thing it is checking into existence."""
        store = FilesystemObjectStore(tmp_path / "objects")
        harness = build_harness(object_store=store)
        # The store's own lock file lives here, so this is a tree, not an
        # empty directory: the point is that the root is gone.
        shutil.rmtree(tmp_path / "objects")

        report = await ReadinessService(harness.context).check()
        assert not report.ready
        assert _by_name(report)["object_store"].ok is False
        assert not (tmp_path / "objects").exists()

    async def test_a_store_with_no_probe_is_unchecked_not_written_to(self) -> None:
        """The fallback used to be a probe write. It is now an honest 'unknown'."""

        class NoPing(InMemoryObjectStore):
            ping = None

        harness = build_harness(object_store=NoPing())
        report = await ReadinessService(harness.context).check()
        assert report.ready
        store_check = _by_name(report)["object_store"]
        assert store_check.blocks_serving is False
        assert "no read-only probe" in store_check.detail


class TestTheRevisionReader:
    """The adapter behind the schema check, against real databases.

    The checks above fake the reader, which proves the policy and nothing about
    the SQL. This is the half that can only be established by running it.
    """

    async def test_it_reads_sqlite(self, tmp_path) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from lab_assistant.adapters.db.migrations import schema_revision_reader

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
        try:
            read = schema_revision_reader(engine)
            # No table at all: a real state, reported as "unknown" rather than
            # raising out of a probe.
            assert await read() is None

            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
                )
                await connection.execute(
                    text("INSERT INTO alembic_version VALUES ('0001_initial')")
                )
            assert await read() == "0001_initial"
        finally:
            await engine.dispose()

    async def test_a_branched_history_is_not_a_single_answer(self, tmp_path) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from lab_assistant.adapters.db.migrations import schema_revision_reader

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'branched.db'}")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
                )
                await connection.execute(text("INSERT INTO alembic_version VALUES ('0002_a')"))
                await connection.execute(text("INSERT INTO alembic_version VALUES ('0002_b')"))
            # Two heads. Reported as both rather than by picking one, so the
            # readiness check says "does not match" instead of agreeing by luck.
            assert await schema_revision_reader(engine)() == "0002_a,0002_b"
        finally:
            await engine.dispose()

    @pytest.mark.postgres
    async def test_it_reads_a_migrated_postgres(self, postgres_url) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from lab_assistant.adapters.db.migrations import (
            expected_revision,
            schema_revision_reader,
        )

        engine = create_async_engine(postgres_url)
        try:
            revision = await schema_revision_reader(engine)()
        finally:
            await engine.dispose()

        if revision is None:
            pytest.skip("LAB_TEST_DATABASE_URL points at a database built without Alembic")
        # The whole point of the check: what the database reports and what this
        # build expects are the same kind of string, and comparable.
        assert revision == expected_revision()


class TestTheChecksAreRoleSpecific:
    """The half of F15 that had to wait for the installation role to exist.

    A tightly scoped reader credential has GET on the objects it serves and no
    bucket-wide LIST. Probing it with a LIST reports an installation unfit while
    every object it actually needs is readable — so the probe has to differ by
    role, not just the thing being probed.
    """

    @pytest.fixture
    async def served(self, tmp_path):
        """A publisher that has published, so there is something to serve."""
        from lab_assistant.contracts.policy import (
            Effect,
            PolicyAction,
            ResourceType,
            SubjectType,
        )
        from lab_assistant.domain.workspace import Role
        from lab_assistant.testing import make_actor, publish_demo_corpus

        harness = build_harness(object_store=FilesystemObjectStore(tmp_path / "objects"))
        user = await harness.accounts.create_user(
            email="steward@lab.invalid", password="correct horse battery"
        )
        workspace, _ = await harness.accounts.create_workspace(
            slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
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
                policy_basis="readiness_role_test",
            )
            for action in (
                PolicyAction.QUERY,
                PolicyAction.VIEW_CONTENT,
                PolicyAction.INDEX,
                PolicyAction.DISPLAY_EXCERPT,
            )
        ]
        actor = make_actor(
            user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
        )
        await publish_demo_corpus(harness, actor)
        return harness

    def _as_reader(self, harness):
        from lab_assistant.domain.installation import InstallationRole, InstanceCapabilities

        return replace(
            harness.context,
            installation=InstanceCapabilities(role=InstallationRole.READER),
        )

    async def test_a_reader_fetches_a_manifest_rather_than_listing(self, served) -> None:
        """The narrowest request that proves the thing in question."""

        class ListingRefused(FilesystemObjectStore):
            """A store whose LIST is denied, as a reader's credential would be."""

            async def ping(self) -> None:
                raise PermissionError("this credential may not list the bucket")

        store = ListingRefused(served.object_store.root)
        context = replace(self._as_reader(served), object_store=store)

        report = await ReadinessService(context).check()
        assert report.ready, [c.detail for c in report.checks if c.blocking_failure]
        assert "manifest" in _by_name(report)["object_store"].detail

    async def test_a_publisher_with_the_same_store_is_not_ready(self, served) -> None:
        """The same denial, the other role: a publisher's credential is supposed
        to be able to list, so a refusal is a real finding."""

        class ListingRefused(FilesystemObjectStore):
            async def ping(self) -> None:
                raise PermissionError("denied")

        context = replace(served.context, object_store=ListingRefused(served.object_store.root))
        report = await ReadinessService(context).check()
        assert not report.ready
        assert _by_name(report)["object_store"].ok is False

    async def test_a_reader_whose_manifest_is_missing_refuses_traffic(self, served) -> None:
        """A reader that cannot fetch what it serves has nothing to answer with."""

        class Empty(FilesystemObjectStore):
            async def head(self, object_id):
                return None

        context = replace(self._as_reader(served), object_store=Empty(served.object_store.root))
        report = await ReadinessService(context).check()
        assert not report.ready
        assert report.http_status == 503
        assert "manifest object" in _by_name(report)["object_store"].detail

    async def test_a_reader_with_nothing_servable_refuses_traffic(self, harness) -> None:
        """An import that has not landed: every question answered with nothing."""
        report = await ReadinessService(self._as_reader(harness)).check()
        assert not report.ready
        assert report.http_status == 503
        corpus = _by_name(report)["corpus"]
        assert corpus.ok is False
        assert "nothing to answer with" in corpus.detail

    async def test_a_publisher_with_nothing_servable_is_still_ready(self, harness) -> None:
        """The same state, and for a publisher it is a normal one: stewards are
        still building the first corpus. Blocking here would mean a fresh
        installation never comes into service."""
        report = await ReadinessService(harness.context).check()
        assert report.ready
        corpus = _by_name(report)["corpus"]
        assert corpus.blocks_serving is False
        assert "still being set up" in corpus.detail

    async def test_a_served_corpus_is_named(self, served) -> None:
        report = await ReadinessService(served.context).check()
        assert report.ready
        assert "serving release" in _by_name(report)["corpus"].detail
