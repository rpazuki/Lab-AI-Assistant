"""The lifecycle over a database that enforces its foreign keys.

The in-memory repositories check workspace scoping and nothing else, so a service
that wrote ``asset.object_id`` without ever creating the ``objects`` row passed
the whole offline suite. PostgreSQL rejects that insert. These tests run the real
lifecycle against SQLite with ``PRAGMA foreign_keys=ON`` — and against
``LAB_TEST_DATABASE_URL`` when it is set — so the gap cannot reopen quietly.

The reachability check walks the schema rather than a hand-written list: every
foreign key into ``objects`` is covered the day it is added, including the ones
nobody remembered to mention here.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from lab_assistant.adapters.connectors.fake import FakeAcquisitionConnector
from lab_assistant.adapters.db import tables as t
from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.adapters.db.search import PostgresSearchIndex
from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.application.scopes import ScopeService
from lab_assistant.config import LabEnv, Settings
from lab_assistant.domain.objects import ObjectRecord
from lab_assistant.domain.scope import ConnectorPlanEntry
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import (
    build_harness,
    fake_source,
    make_actor,
    publish_demo_corpus,
    reset_database,
    source_page,
)

SCOPE_QUERY = "yarrowia lipolytica lipid production"

DATABASE_URLS = ["sqlite+aiosqlite:///:memory:"]
if os.environ.get("LAB_TEST_DATABASE_URL"):
    DATABASE_URLS.append(os.environ["LAB_TEST_DATABASE_URL"])


@pytest.fixture(params=DATABASE_URLS, ids=lambda url: url.split(":")[0])
async def sql_harness(request, tmp_path, fixture_records: list[dict[str, Any]], jats_bytes: bytes):
    """The ordinary harness, wired to a real database and a real object store."""
    settings = Settings(
        env=LabEnv.LOCAL,
        database_url=request.param,
        object_store_root=tmp_path / "objects",
        llm_provider="fake",
        embedding_profile="hashing-v1",
    )
    engine = make_engine(settings)
    await create_schema(engine, dimensions=(64,))
    # A shared PostgreSQL database plus a deterministic id factory means the
    # second test starts with the first test's rows and the first test's UUIDs.
    await reset_database(engine)
    harness = build_harness(
        connectors={
            "pubmed-fixture": fake_source(
                "pubmed-fixture", [source_page(query=SCOPE_QUERY, records=fixture_records)]
            ),
            "fake-acquire": FakeAcquisitionConnector(
                assets={"doi:10.1234/example": (jats_bytes, "application/xml", "cc-by-4.0")}
            ),
        },
        uow_factory=sql_uow_factory(engine),
        object_store=FilesystemObjectStore(settings.object_store_root),
        search_index=PostgresSearchIndex(engine),
    )
    harness.extras["engine"] = engine
    try:
        yield harness
    finally:
        await engine.dispose()


@pytest.fixture
async def sql_actor(sql_harness):
    steward = await sql_harness.accounts.create_user(
        email="steward@lab.invalid", password="correct horse battery"
    )
    workspace, _membership = await sql_harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=steward.id
    )
    return make_actor(user_id=steward.id, workspace_id=workspace.id, role=Role.ADMIN)


def _object_foreign_keys() -> list[tuple[str, str]]:
    """Every ``(table, column)`` in the schema that points at ``objects.id``."""
    found: list[tuple[str, str]] = []
    for table in t.METADATA.tables.values():
        for column in table.columns:
            for key in column.foreign_keys:
                if key.column is t.objects.c.id:
                    found.append((table.name, column.name))
    return sorted(found)


async def _unresolved_references(engine) -> list[str]:
    """References to objects the registry does not contain."""
    broken: list[str] = []
    async with engine.connect() as connection:
        registered = {
            row[0] for row in (await connection.execute(select(t.objects.c.id))).fetchall()
        }
        for table_name, column_name in _object_foreign_keys():
            table = t.METADATA.tables[table_name]
            column = table.c[column_name]
            rows = await connection.execute(select(column).where(column.is_not(None)))
            for (value,) in rows.fetchall():
                identifier = value if isinstance(value, UUID) else UUID(str(value))
                if identifier not in registered:
                    broken.append(f"{table_name}.{column_name} -> {identifier}")
    return broken


class TestObjectRegistry:
    async def test_the_schema_really_does_depend_on_the_registry(self) -> None:
        """If this shrinks to nothing, the reachability test proves nothing."""
        keys = _object_foreign_keys()
        assert ("assets", "object_id") in keys
        assert ("renditions", "object_id") in keys
        assert ("source_requests", "response_body_object_id") in keys
        assert len(keys) >= 10

    async def test_publishing_a_corpus_registers_every_object_it_references(
        self, sql_harness, sql_actor
    ) -> None:
        """Assets, renditions, the manifest and the validation report.

        Before the registry existed this failed at the first asset insert with a
        foreign-key violation, on PostgreSQL and on any SQLite that was asked to
        enforce its constraints.
        """
        await publish_demo_corpus(sql_harness, sql_actor)

        engine = sql_harness.extras["engine"]
        assert await _unresolved_references(engine) == []

        async with sql_harness.context.uow_factory() as uow:
            count = await uow.objects.count_objects(sql_actor.workspace_id)
            classes = {
                record.object_class
                for record in await uow.objects.list_objects(sql_actor.workspace_id, limit=500)
            }
        assert count >= 4
        assert {"asset", "rendition", "release_manifest"} <= classes

    async def test_discovery_registers_source_responses_and_canonical_records(
        self, sql_harness, sql_actor
    ) -> None:
        """The provenance objects, which no asset or rendition points at."""
        scopes = ScopeService(sql_harness.context)
        scope = await scopes.create_version(
            sql_actor,
            scope_id=None,
            name="Yarrowia lipid production",
            scientific_objective="Lipid yields in engineered oleaginous yeast.",
            connector_plan=[
                ConnectorPlanEntry(
                    connector_id="pubmed-fixture", connector_version="1.0.0", query=SCOPE_QUERY
                )
            ],
            intended_uses=("internal literature search",),
            concepts=("lipid production",),
        )
        approved = await scopes.approve(sql_actor, scope_version_id=scope.id)

        from lab_assistant.application.discovery import DiscoveryService

        run, tasks = await DiscoveryService(sql_harness.context).start_run(
            sql_actor, scope_version_id=approved.id
        )
        await sql_harness.orchestrator.submit_requests(
            tasks,
            workspace_id=sql_actor.workspace_id,
            actor_id=sql_actor.user_id,
            run_id=run.id,
        )
        await sql_harness.run_all_tasks()

        engine = sql_harness.extras["engine"]
        assert await _unresolved_references(engine) == []

        async with sql_harness.context.uow_factory() as uow:
            classes = {
                record.object_class
                for record in await uow.objects.list_objects(sql_actor.workspace_id, limit=500)
            }
        assert {"source_response", "canonical_record"} <= classes

    async def test_the_registry_describes_what_the_store_holds(
        self, sql_harness, sql_actor
    ) -> None:
        """Digest, length and key, so a restore can say what is missing and where."""
        await publish_demo_corpus(sql_harness, sql_actor)
        async with sql_harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(sql_actor.workspace_id, limit=500)
        assert records
        for record in records:
            stored = await sql_harness.object_store.head(record.id)
            assert stored is not None, f"registered object {record.id} is not in the store"
            assert stored.sha256 == record.sha256
            assert stored.byte_length == record.byte_length
            assert stored.key == record.storage_key

    async def test_registering_the_same_bytes_again_cannot_clear_a_tombstone(
        self, sql_harness, sql_actor
    ) -> None:
        """A repeat of an ordinary write must not resurrect denied content."""
        stored = await sql_harness.object_store.put(
            b"a retracted figure",
            workspace_id=sql_actor.workspace_id,
            object_class="asset",
            media_type="application/octet-stream",
        )
        async with sql_harness.context.uow_factory() as uow:
            from lab_assistant.application.objects import register_object

            await register_object(uow, stored)
            await uow.objects.tombstone(stored.id, reason="rights withdrawn")
            await uow.commit()

        async with sql_harness.context.uow_factory() as uow:
            from lab_assistant.application.objects import object_record

            await uow.objects.register(object_record(stored))
            await uow.commit()

        async with sql_harness.context.uow_factory() as uow:
            after = await uow.objects.get(stored.id)
        assert after is not None
        assert after.tombstoned is True
        assert after.tombstone_reason == "rights withdrawn"

    async def test_the_registry_pages_with_a_keyset_cursor(self, sql_harness, sql_actor) -> None:
        """A limit is a page size, so an inventory sweep must see every row."""
        from lab_assistant.application.objects import register_object

        for index in range(7):
            stored = await sql_harness.object_store.put(
                f"payload {index}".encode(),
                workspace_id=sql_actor.workspace_id,
                object_class="asset",
            )
            async with sql_harness.context.uow_factory() as uow:
                await register_object(uow, stored)
                await uow.commit()

        seen: list[ObjectRecord] = []
        cursor: UUID | None = None
        while True:
            async with sql_harness.context.uow_factory() as uow:
                page = await uow.objects.list_objects(
                    sql_actor.workspace_id, limit=3, after_id=cursor
                )
            if not page:
                break
            seen.extend(page)
            cursor = page[-1].id
        assert len(seen) == 7
        assert len({record.id for record in seen}) == 7
