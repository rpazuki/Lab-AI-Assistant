"""The SQL adapter, exercised for real.

Runs against SQLite offline (so CI proves the mapping works) and against
PostgreSQL when ``LAB_TEST_DATABASE_URL`` is set (so the constraints, the partial
unique index, and ``FOR UPDATE SKIP LOCKED`` are exercised too).

The same acceptance-shaped flow runs here as over the in-memory adapter, which is
the point: the adapters must be interchangeable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.adapters.db.search import PostgresSearchIndex
from lab_assistant.adapters.models.fake import FakeLLMProvider, HashingEmbeddingProvider
from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.adapters.parsers.registry import default_parser_registry
from lab_assistant.application import build_handler_registry
from lab_assistant.application.accounts import AccountService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.tasks import TaskOrchestrator
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.runtime import SequentialIds, system_clock
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.identity import Work, WorkIdentifier, WorkKind
from lab_assistant.domain.states import RelevanceState, TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Role
from lab_assistant.policy.engine import PolicyEngine
from lab_assistant.policy.model_transfer import ModelTransferPolicy
from lab_assistant.policy.rights import default_open_access_policy
from lab_assistant.testing import make_actor, reset_database

DATABASE_URLS = ["sqlite+aiosqlite:///:memory:"]
if os.environ.get("LAB_TEST_DATABASE_URL"):
    DATABASE_URLS.append(os.environ["LAB_TEST_DATABASE_URL"])


@pytest.fixture(params=DATABASE_URLS, ids=lambda url: url.split(":")[0])
async def sql_stack(request, tmp_path):
    settings = Settings(
        env=LabEnv.LOCAL,
        database_url=request.param,
        object_store_root=tmp_path / "objects",
        llm_provider="fake",
        embedding_profile="hashing-v1",
    )
    engine = make_engine(settings)
    await create_schema(engine, dimensions=(64,))
    await reset_database(engine)
    context = ServiceContext(
        uow_factory=sql_uow_factory(engine),
        object_store=FilesystemObjectStore(settings.object_store_root),
        clock=system_clock,
        id_factory=SequentialIds(),
        policy_engine=PolicyEngine(),
        rights_policy=default_open_access_policy(),
        model_transfer_policy=ModelTransferPolicy(),
        settings=settings,
        search_index=PostgresSearchIndex(engine),
        parsers=default_parser_registry(),
        llm_provider=FakeLLMProvider(),
        embedding_provider=HashingEmbeddingProvider(dimensions=64),
    )
    handlers = build_handler_registry(context)
    try:
        yield {
            "engine": engine,
            "context": context,
            "handlers": handlers,
            "orchestrator": TaskOrchestrator(context, handlers),
            "accounts": AccountService(context),
        }
    finally:
        await engine.dispose()


class TestAccountsRoundTrip:
    async def test_user_and_workspace_persist(self, sql_stack) -> None:
        accounts = sql_stack["accounts"]
        user = await accounts.create_user(
            email="Steward@Lab.invalid", password="a-long-passphrase", full_name="A Steward"
        )
        workspace, membership = await accounts.create_workspace(
            slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
        )
        async with sql_stack["context"].uow_factory() as uow:
            loaded = await uow.users.get(user.id)
            by_email = await uow.users.get_by_email("steward@lab.invalid")
            memberships = await uow.workspaces.list_memberships(workspace.id)
            for_user = await uow.workspaces.list_for_user(user.id)
        assert loaded is not None and loaded.full_name == "A Steward"
        assert by_email is not None and by_email.id == user.id
        assert [m.id for m in memberships] == [membership.id]
        assert [w.slug for w in for_user] == ["rla-lab"]

    async def test_authentication_round_trips_through_the_database(self, sql_stack) -> None:
        accounts = sql_stack["accounts"]
        await accounts.create_user(email="s@lab.invalid", password="a-long-passphrase")
        authenticated = await accounts.authenticate(
            email="s@lab.invalid", password="a-long-passphrase"
        )
        assert authenticated.last_login_at is not None

    async def test_groups_keep_their_members(self, sql_stack) -> None:
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        member = await accounts.create_user(email="m@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        await accounts.add_member(admin, user_id=member.id, role=Role.RESEARCHER)
        group = await accounts.create_group(
            admin,
            slug="restricted",
            display_name="Restricted",
            member_user_ids=frozenset({owner.id, member.id}),
        )
        async with sql_stack["context"].uow_factory() as uow:
            groups = await uow.workspaces.list_groups(workspace.id, user_id=member.id)
        assert [g.id for g in groups] == [group.id]
        assert groups[0].member_user_ids == frozenset({owner.id, member.id})

    async def test_grants_round_trip_with_their_enums(self, sql_stack) -> None:
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        grant = await accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=PolicyAction.QUERY,
            effect=Effect.ALLOW,
            policy_basis="test",
        )
        async with sql_stack["context"].uow_factory() as uow:
            grants = await uow.policies.list_grants(workspace.id, subject_ids=[workspace.id])
        assert [g.id for g in grants] == [grant.id]
        assert grants[0].action is PolicyAction.QUERY
        assert grants[0].effect is Effect.ALLOW


class TestIdentityConstraints:
    async def test_one_active_work_per_strong_identifier(self, sql_stack) -> None:
        """The invariant is a database index, not application discipline."""
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )

        async def _add_work_with_doi(doi: str) -> Work:
            async with context.uow_factory() as uow:
                work = Work(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    work_kind=WorkKind.ARTICLE,
                    created_at=datetime.now(tz=UTC),
                )
                await uow.works.add(work)
                await uow.works.add_identifier(
                    WorkIdentifier(
                        id=context.new_id(),
                        workspace_id=workspace.id,
                        work_id=work.id,
                        scheme=IdentifierScheme.DOI,
                        value_normalized=doi,
                        value_display=doi,
                    )
                )
                await uow.commit()
            return work

        first = await _add_work_with_doi("10.1/x")
        with pytest.raises(IntegrityError):
            await _add_work_with_doi("10.1/x")

        async with context.uow_factory() as uow:
            found = await uow.works.find_by_identifier(
                workspace.id, scheme="doi", value_normalized="10.1/x"
            )
        assert found is not None and found.id == first.id

    async def test_workspace_scoping_is_enforced_by_the_query(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        left, _ = await accounts.create_workspace(
            slug="left", display_name="Left", owner_user_id=owner.id
        )
        right, _ = await accounts.create_workspace(
            slug="right", display_name="Right", owner_user_id=owner.id
        )
        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=left.id,
                work_kind=WorkKind.ARTICLE,
                created_at=datetime.now(tz=UTC),
            )
            await uow.works.add(work)
            await uow.commit()
        async with context.uow_factory() as uow:
            assert await uow.works.get(left.id, work.id) is not None
            assert await uow.works.get(right.id, work.id) is None


class TestTransactions:
    async def test_leaving_without_committing_rolls_back(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        work_id = context.new_id()
        async with context.uow_factory() as uow:
            await uow.works.add(
                Work(
                    id=work_id,
                    workspace_id=workspace.id,
                    work_kind=WorkKind.ARTICLE,
                    created_at=datetime.now(tz=UTC),
                )
            )
            # No commit: the write must not survive.
        async with context.uow_factory() as uow:
            assert await uow.works.get(workspace.id, work_id) is None

    async def test_an_exception_rolls_back(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        work_id = context.new_id()
        with pytest.raises(RuntimeError):
            async with context.uow_factory() as uow:
                await uow.works.add(
                    Work(
                        id=work_id,
                        workspace_id=workspace.id,
                        work_kind=WorkKind.ARTICLE,
                        created_at=datetime.now(tz=UTC),
                    )
                )
                raise RuntimeError("something failed mid-transaction")
        async with context.uow_factory() as uow:
            assert await uow.works.get(workspace.id, work_id) is None


class TestTaskEngineOverSql:
    async def test_claim_respects_the_stage_cap_and_lease(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        now = datetime.now(tz=UTC)
        async with context.uow_factory() as uow:
            for index in range(2):
                await uow.tasks.add(
                    Task(
                        id=context.new_id(),
                        workspace_id=workspace.id,
                        stage=StageName.INDEXING,
                        state="ready",
                        input_fingerprint=f"task:{index}",
                        inputs={"release_id": str(uuid4())},
                        created_at=now,
                    )
                )
            await uow.commit()

        async with context.uow_factory() as uow:
            first = await uow.tasks.claim_next(
                owner="worker-a",
                worker_class="compute",
                now=now,
                stages=(StageName.INDEXING,),
                stage_concurrency={StageName.INDEXING: 1},
            )
            await uow.commit()
        assert first is not None

        async with context.uow_factory() as uow:
            blocked = await uow.tasks.claim_next(
                owner="worker-b",
                worker_class="compute",
                now=now,
                stages=(StageName.INDEXING,),
                stage_concurrency={StageName.INDEXING: 1},
            )
        assert blocked is None

    async def test_fingerprint_uniqueness_is_enforced(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        now = datetime.now(tz=UTC)

        async def _add(task_id) -> None:
            async with context.uow_factory() as uow:
                await uow.tasks.add(
                    Task(
                        id=task_id,
                        workspace_id=workspace.id,
                        stage=StageName.PARSING,
                        state="ready",
                        input_fingerprint="task:same",
                        created_at=now,
                    )
                )
                await uow.commit()

        await _add(context.new_id())
        with pytest.raises(IntegrityError):
            await _add(context.new_id())


class TestSearchProjectionOverSql:
    async def test_chunks_embeddings_and_copies_round_trip(self, sql_stack) -> None:
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        search = context.search_index
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        release_a, release_b = context.new_id(), context.new_id()
        work_id = context.new_id()
        rendition_id = context.new_id()

        from lab_assistant.domain.release import CorpusRelease

        async with context.uow_factory() as uow:
            for release_id in (release_a, release_b):
                await uow.releases.add(
                    CorpusRelease(
                        id=release_id,
                        workspace_id=workspace.id,
                        name="corpus",
                        semantic_version=f"1.{release_id.int % 10}.0",
                        created_at=datetime.now(tz=UTC),
                    )
                )
            await uow.commit()

        rows = [
            {
                "chunk_key": "chunk:one",
                "work_id": str(work_id),
                "rendition_id": str(rendition_id),
                "node_ids": ["p-1"],
                "content": "Strain YL-A reached a lipid yield of 0.42 g/g substrate.",
                "content_sha256": "a" * 64,
                "chunk_type": "paragraph",
                "section_label": "Results",
                "page": None,
                "token_count": 12,
                "chunker_id": "structural-v1",
                "chunker_version": "1",
                "metadata": {},
            }
        ]
        assert await search.write_chunks(workspace.id, release_a, rows) == 1
        # Writing the same content twice is a no-op, not a duplicate.
        await search.write_chunks(workspace.id, release_a, rows)
        assert await search.count_chunks(workspace.id, release_a) == 1

        embedder = context.embedding_provider
        vector = await embedder.embed_query(rows[0]["content"])
        await search.write_embeddings(
            workspace.id,
            release_a,
            profile_id="hashing-v1",
            rows=[
                {
                    "chunk_key": "chunk:one",
                    "work_id": str(work_id),
                    "vector": list(vector),
                }
            ],
        )

        lexical = await search.lexical_search(
            workspace.id, release_a, query="lipid yield", top_k=5, work_ids=[work_id]
        )
        assert lexical and lexical[0]["chunk_key"] == "chunk:one"

        vectors = await search.vector_search(
            workspace.id,
            release_a,
            profile_id="hashing-v1",
            vector=list(vector),
            top_k=5,
            work_ids=[work_id],
        )
        assert vectors and vectors[0]["chunk_key"] == "chunk:one"

        # Policy filtering happens in the candidate SQL: an empty allowlist yields
        # nothing rather than everything.
        assert (
            await search.lexical_search(
                workspace.id, release_a, query="lipid", top_k=5, work_ids=[]
            )
            == []
        )

        copied = await search.copy_chunks(
            workspace.id, from_release_id=release_a, to_release_id=release_b, work_ids=[work_id]
        )
        assert copied == 1
        assert await search.count_chunks(workspace.id, release_b) == 1

    async def test_text_with_nul_characters_is_stored(self, sql_stack) -> None:
        """PDF text can carry NUL where a glyph has no mapping; Postgres refuses it.

        One such character in one paper failed a whole release's index build on
        the deployment, five attempts in a row.
        """
        from lab_assistant.contracts.parsing import (
            NodeKind,
            RenditionDocument,
            RenditionNode,
            TableCell,
        )
        from lab_assistant.domain.chunking import chunk_rendition
        from lab_assistant.domain.release import CorpusRelease

        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        release_id = context.new_id()
        async with context.uow_factory() as uow:
            await uow.releases.add(
                CorpusRelease(
                    id=release_id,
                    workspace_id=workspace.id,
                    name="corpus",
                    semantic_version="1.0.0",
                    created_at=datetime.now(tz=UTC),
                )
            )
            await uow.commit()
        document = RenditionDocument(
            format="pdf",
            nodes=(
                RenditionNode(
                    node_id="s-1",
                    kind=NodeKind.SECTION,
                    title="Classi\x00cation",
                    children=(
                        RenditionNode(
                            node_id="p-1",
                            kind=NodeKind.PARAGRAPH,
                            text="Strains were classi\x00ed by lipid yield\x00.",
                        ),
                        RenditionNode(
                            node_id="t-1",
                            kind=NodeKind.TABLE,
                            title="E\x00ciency",
                            cells=(
                                TableCell(
                                    node_id="c-1", row=0, column=0, is_header=True, text="Yield"
                                ),
                                TableCell(node_id="c-2", row=1, column=0, text="0.4\x002"),
                            ),
                        ),
                    ),
                ),
            ),
        )
        chunks = chunk_rendition(document, release_item_digest="d", rendition_digest="r")
        work_id, rendition_id = context.new_id(), context.new_id()
        rows = [
            {
                "chunk_key": chunk.chunk_key,
                "work_id": str(work_id),
                "rendition_id": str(rendition_id),
                "node_ids": list(chunk.node_ids),
                "content": chunk.content,
                "content_sha256": chunk.content_sha256,
                "chunk_type": chunk.chunk_type,
                "section_label": chunk.section_label,
                "page": None,
                "token_count": chunk.token_count,
                "chunker_id": "structural-v1",
                "chunker_version": "1",
                "metadata": dict(chunk.metadata),
            }
            for chunk in chunks
        ]

        assert await context.search_index.write_chunks(workspace.id, release_id, rows) == 2
        contents = sorted(chunk.content for chunk in chunks)
        assert contents == ["Eciency — Yield: 0.42", "Strains were classied by lipid yield."]
        assert {chunk.section_label for chunk in chunks} == {"Classication"}
        found = await context.search_index.lexical_search(
            workspace.id, release_id, query="lipid yield", top_k=5, work_ids=[work_id]
        )
        assert found and "\x00" not in found[0]["content"]

    async def test_vector_search_is_scoped_to_one_release(self, sql_stack) -> None:
        """A chunk key is content-derived, so releases share keys by design.

        The vector table used to be joined to the chunk table on ``chunk_key``
        alone. Once an unchanged item was copied into a successor release — which is
        exactly what makes an incremental build cheap — every vector hit matched
        two chunk rows: inflated candidate counts, and a citation that could resolve
        against a stale release.
        """
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        search = context.search_index
        owner = await accounts.create_user(email="v@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="vectors", display_name="Vectors", owner_user_id=owner.id
        )
        release_a, release_b = context.new_id(), context.new_id()
        work_id, rendition_id = context.new_id(), context.new_id()

        from lab_assistant.domain.release import CorpusRelease

        async with context.uow_factory() as uow:
            for index, release_id in enumerate((release_a, release_b)):
                await uow.releases.add(
                    CorpusRelease(
                        id=release_id,
                        workspace_id=workspace.id,
                        name="corpus",
                        semantic_version=f"2.{index}.0",
                        created_at=datetime.now(tz=UTC),
                    )
                )
            await uow.commit()

        row = {
            "chunk_key": "chunk:shared",
            "work_id": str(work_id),
            "rendition_id": str(rendition_id),
            "node_ids": ["p-1"],
            "content": "Strain YL-B reached a lipid titre of 12 g/L on glucose.",
            "content_sha256": "b" * 64,
            "chunk_type": "paragraph",
            "section_label": "Results",
            "page": None,
            "token_count": 12,
            "chunker_id": "structural-v1",
            "chunker_version": "1",
            "metadata": {},
        }
        vector = list(await context.embedding_provider.embed_query(row["content"]))
        for release_id in (release_a, release_b):
            await search.write_chunks(workspace.id, release_id, [row])
            await search.write_embeddings(
                workspace.id,
                release_id,
                profile_id="hashing-v1",
                rows=[{"chunk_key": row["chunk_key"], "work_id": str(work_id), "vector": vector}],
            )

        hits = await search.vector_search(
            workspace.id, release_a, profile_id="hashing-v1", vector=vector, top_k=10
        )
        assert len(hits) == 1
        assert hits[0]["release_id"] == str(release_a)

        other = await search.vector_search(
            workspace.id, release_b, profile_id="hashing-v1", vector=vector, top_k=10
        )
        assert [hit["release_id"] for hit in other] == [str(release_b)]

    async def test_counts_and_claims_obey_the_work_filter(self, sql_stack) -> None:
        """Aggregates are a disclosure channel; they take the same allowlist."""
        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        search = context.search_index
        owner = await accounts.create_user(email="c@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="counts", display_name="Counts", owner_user_id=owner.id
        )
        release_id = context.new_id()
        visible, restricted = context.new_id(), context.new_id()

        from lab_assistant.domain.release import CorpusRelease

        async with context.uow_factory() as uow:
            await uow.releases.add(
                CorpusRelease(
                    id=release_id,
                    workspace_id=workspace.id,
                    name="corpus",
                    semantic_version="3.0.0",
                    created_at=datetime.now(tz=UTC),
                )
            )
            await uow.commit()

        def chunk(work_id, key: str) -> dict:
            return {
                "chunk_key": key,
                "work_id": str(work_id),
                "rendition_id": str(context.new_id()),
                "node_ids": ["p-1"],
                "content": f"content {key}",
                "content_sha256": key.ljust(64, "0"),
                "chunk_type": "paragraph",
                "section_label": None,
                "page": None,
                "token_count": 4,
                "chunker_id": "structural-v1",
                "chunker_version": "1",
                "metadata": {},
            }

        await search.write_chunks(
            workspace.id,
            release_id,
            [chunk(visible, "visible"), chunk(restricted, "restricted")],
        )
        await search.write_claim_projection(
            workspace.id,
            release_id,
            [
                {
                    "revision_id": str(context.new_id()),
                    "claim_id": str(context.new_id()),
                    "work_id": str(work_id),
                    "predicate": "lipid_yield",
                    "subject": "YL",
                    "object_kind": "quantity",
                    "value": "0.21",
                    "unit": "g/g",
                    "review_status": "accepted",
                    "abstention_reason": "reported",
                    "evidence_span_ids": [],
                }
                for work_id in (visible, restricted)
            ],
        )

        assert await search.count_chunks(workspace.id, release_id) == 2
        assert await search.count_chunks(workspace.id, release_id, work_ids=[visible]) == 1
        assert await search.count_chunks(workspace.id, release_id, work_ids=[]) == 0

        claims = await search.claim_search(workspace.id, release_id, top_k=10, work_ids=[visible])
        assert [claim["work_id"] for claim in claims] == [str(visible)]
        assert await search.claim_search(workspace.id, release_id, top_k=10, work_ids=[]) == []


class TestEndToEndOverSql:
    async def test_release_publish_and_answer(self, sql_stack) -> None:
        """The same flow as the in-memory acceptance test, over SQL."""
        from lab_assistant.contracts.canonical import canonical_json
        from lab_assistant.contracts.parsing import (
            NodeKind,
            ParseQualityMetrics,
            RenditionDocument,
            RenditionNode,
        )
        from lab_assistant.domain.assets import (
            AcquisitionRoute,
            Asset,
            MalwareScanStatus,
            ValidationStatus,
        )
        from lab_assistant.domain.renditions import ParseRun, Rendition
        from lab_assistant.domain.states import ParseState

        context = sql_stack["context"]
        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="o@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="lab", display_name="Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        grants = []
        for action in (
            PolicyAction.QUERY,
            PolicyAction.VIEW_CONTENT,
            PolicyAction.DISPLAY_EXCERPT,
            PolicyAction.INDEX,
        ):
            grants.append(
                await accounts.grant(
                    admin,
                    subject_type=SubjectType.WORKSPACE,
                    subject_id=workspace.id,
                    resource_type=ResourceType.WORKSPACE,
                    resource_id=workspace.id,
                    action=action,
                    effect=Effect.ALLOW,
                    policy_basis="workspace_corpus",
                )
            )
        actor = make_actor(
            user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
        )

        text_body = "Strain YL-A reached a lipid yield of 0.42 g/g substrate at 30 degrees C."
        document = RenditionDocument(
            format="jats",
            nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=text_body),),
            quality=ParseQualityMetrics(characters=len(text_body), section_count=1),
        )
        rendition_object = await context.object_store.put(
            canonical_json(document.model_dump(mode="json")),
            workspace_id=workspace.id,
            object_class="rendition",
            media_type="application/json",
        )
        asset_object = await context.object_store.put(
            text_body.encode(), workspace_id=workspace.id, object_class="asset"
        )

        from lab_assistant.application.acquisition import AcquisitionService

        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_kind=WorkKind.ARTICLE,
                relevance=RelevanceState.INCLUDED,
                created_at=datetime.now(tz=UTC),
            )
            await uow.works.add(work)
            asset = Asset(
                id=context.new_id(),
                workspace_id=workspace.id,
                sha256=asset_object.sha256,
                byte_length=asset_object.byte_length,
                object_id=asset_object.id,
                media_type_detected="application/xml",
                malware_scan_status=MalwareScanStatus.PASSED,
                validation_status=ValidationStatus.PASSED,
                work_id=work.id,
                identity_verified=True,
                created_at=datetime.now(tz=UTC),
            )
            await register_object(uow, asset_object)
            await uow.assets.add_asset(asset)
            await uow.commit()

        rights = await AcquisitionService(context).decide_rights(
            actor,
            subject_type="asset",
            subject_id=asset.id,
            licence_expression="cc-by-4.0",
            access_basis="open_access_repository",
            route=AcquisitionRoute.OPEN_ACCESS_API,
            requested_actions=frozenset(
                {PolicyAction.PROCESS_LOCAL, PolicyAction.INDEX, PolicyAction.QUERY}
            ),
        )
        async with context.uow_factory() as uow:
            asset.rights_decision_id = rights.id
            await uow.assets.update_asset(asset)
            parse_run = ParseRun(
                id=context.new_id(),
                workspace_id=workspace.id,
                asset_id=asset.id,
                parser_id="jats-etree",
                parser_version="1.0.0",
                input_sha256=asset.sha256,
                run_fingerprint="parse_run:sql",
                status="succeeded",
            )
            await uow.renditions.add_parse_run(parse_run)
            await register_object(uow, rendition_object)
            await uow.renditions.add_rendition(
                Rendition(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    parse_run_id=parse_run.id,
                    asset_id=asset.id,
                    work_id=work.id,
                    object_id=rendition_object.id,
                    sha256=rendition_object.sha256,
                    quality_status=ParseState.READY,
                    created_at=datetime.now(tz=UTC),
                )
            )
            await uow.commit()

        releases = ReleaseService(context)
        release, manifest, _diff = await releases.draft(
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        assert manifest.items
        report = await releases.validate(actor, release_id=release.id)
        assert report.passed, report.failures
        _approved, tasks = await releases.approve(actor, release_id=release.id)
        submitted = await sql_stack["orchestrator"].submit_requests(
            tasks, workspace_id=workspace.id, actor_id=owner.id
        )
        for task in submitted:
            await sql_stack["orchestrator"].execute(task)
        await releases.publish(actor, release_id=release.id)

        chat = ChatService(context)
        session = await chat.create_session(actor)
        answer = await chat.ask(
            actor, session_id=session.id, question="What lipid yield was reported?"
        )
        assert answer.message.citations
        assert answer.message.citation_validation_passed
        resolved = await chat.resolve_citation(
            actor, message_id=answer.message.id, handle=answer.message.citations[0].handle
        )
        assert resolved["resolved"], resolved

        # Audit chain survives a round trip through SQL.
        from lab_assistant.provenance.audit import verify_chain

        async with context.uow_factory() as uow:
            events = await uow.audit.list_events(workspace.id, limit=500)
        ok, problems = verify_chain(events)
        assert ok, problems


class TestEvaluationsOverSql:
    async def test_benchmarks_and_run_lifecycle_round_trip(self, sql_stack) -> None:
        from datetime import UTC, datetime, timedelta

        accounts, context = sql_stack["accounts"], sql_stack["context"]
        user = await accounts.create_user(email="e@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="eval-lab", display_name="Eval Lab", owner_user_id=user.id
        )
        started = datetime(2026, 11, 19, 10, tzinfo=UTC)
        older, newer = context.id_factory(), context.id_factory()
        async with context.uow_factory() as uow:
            await uow.evaluations.add_benchmark(
                {
                    "id": context.id_factory(),
                    "workspace_id": workspace.id,
                    "benchmark_id": "lab-retrieval",
                    "version": "1.0.0",
                    "kind": "retrieval",
                    "checksum": "c1",
                    "sensitivity": "internal",
                    "definition": {"cases": [{"case_id": "q1"}]},
                    "registered_by": user.id,
                }
            )
            for run_id, offset in ((older, 0), (newer, 5)):
                await uow.evaluations.add_run(
                    {
                        "id": run_id,
                        "workspace_id": workspace.id,
                        "benchmark": "lab-retrieval@1.0.0",
                        "benchmark_checksum": "c1",
                        "status": "queued",
                        "kind": "retrieval",
                        "requested_by": user.id,
                        "created_at": started + timedelta(minutes=offset),
                    }
                )
            await uow.evaluations.update_run(
                {
                    "id": older,
                    "workspace_id": workspace.id,
                    "status": "succeeded",
                    "metrics": {"recall@5": 0.5},
                    "gates": {"retrieval:recall@5": True},
                    "failures": ["one miss"],
                    "per_case": [{"case_id": "q1", "unresolved_relevant": ["pmid:1"]}],
                    "completed_at": started + timedelta(minutes=2),
                }
            )
            await uow.commit()

        async with context.uow_factory() as uow:
            benchmark = await uow.evaluations.get_benchmark(workspace.id, "lab-retrieval", "1.0.0")
            listed = await uow.evaluations.list_runs(workspace.id)
            finished = await uow.evaluations.get_run(workspace.id, older)
        assert benchmark is not None and benchmark["definition"] == {"cases": [{"case_id": "q1"}]}
        # Newest first, so the run just queued is at the top of the page.
        assert [row["id"] for row in listed] == [newer, older]
        assert finished is not None
        assert finished["status"] == "succeeded"
        assert finished["failures"] == ["one miss"]
        assert finished["per_case"][0]["unresolved_relevant"] == ["pmid:1"]
        assert finished["requested_by"] == user.id

    async def test_reviews_and_release_gates_round_trip(self, sql_stack) -> None:
        from datetime import UTC, datetime

        from lab_assistant.domain.chat import ChatMessage, ChatSession, MessageRole

        accounts, context = sql_stack["accounts"], sql_stack["context"]
        user = await accounts.create_user(email="r@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="review-lab", display_name="Review Lab", owner_user_id=user.id
        )
        now = datetime(2026, 11, 20, 9, tzinfo=UTC)
        run_id, review_id = context.id_factory(), context.id_factory()
        session = ChatSession(
            id=context.id_factory(),
            workspace_id=workspace.id,
            user_id=user.id,
            created_at=now,
            updated_at=now,
            deleted_at=now,
        )
        async with context.uow_factory() as uow:
            await uow.evaluations.add_run(
                {
                    "id": run_id,
                    "workspace_id": workspace.id,
                    "benchmark": "gen@1",
                    "benchmark_checksum": "c",
                    "status": "succeeded",
                    "kind": "generation",
                    "created_at": now,
                }
            )
            await uow.evaluations.add_review(
                {
                    "id": review_id,
                    "workspace_id": workspace.id,
                    "run_id": run_id,
                    "case_id": "q1",
                    "assigned_to": user.id,
                    "assigned_by": user.id,
                    "created_at": now,
                }
            )
            await uow.evaluations.update_review(
                {
                    "id": review_id,
                    "workspace_id": workspace.id,
                    "status": "submitted",
                    "scores": {"correctness": 4},
                    "flags": ["corpus_gap"],
                    "reviewer_confidence": 3,
                    "comment": "ok",
                    "submitted_at": now,
                }
            )
            await uow.evaluations.set_release_gate(
                {
                    "workspace_id": workspace.id,
                    "benchmark_id": "gen",
                    "version": "1",
                    "set_by": user.id,
                    "set_at": now,
                }
            )
            # Requiring a new version replaces the old requirement.
            await uow.evaluations.set_release_gate(
                {
                    "workspace_id": workspace.id,
                    "benchmark_id": "gen",
                    "version": "2",
                    "set_by": user.id,
                    "set_at": now,
                }
            )
            # Usage counts a deleted session's answers.
            await uow.chat.add_session(session)
            await uow.chat.add_message(
                ChatMessage(
                    id=context.id_factory(),
                    workspace_id=workspace.id,
                    session_id=session.id,
                    role=MessageRole.ASSISTANT,
                    content="a",
                    input_tokens=30,
                    output_tokens=12,
                    latency_ms=400,
                    created_at=now,
                )
            )
            await uow.commit()

        async with context.uow_factory() as uow:
            [review] = await uow.evaluations.list_reviews(workspace.id, assigned_to=user.id)
            gates = await uow.evaluations.list_release_gates(workspace.id)
            usage = await uow.chat.usage_by_user(workspace.id)
        assert review["status"] == "submitted"
        assert review["scores"] == {"correctness": 4}
        assert review["flags"] == ["corpus_gap"]
        assert [(gate["benchmark_id"], gate["version"]) for gate in gates] == [("gen", "2")]
        totals = usage[user.id]
        assert (totals.sessions, totals.answers, totals.total_tokens) == (1, 1, 42)
        assert totals.avg_latency_ms == 400

        async with context.uow_factory() as uow:
            await uow.evaluations.clear_release_gate(workspace.id, "gen")
            await uow.commit()
        async with context.uow_factory() as uow:
            assert await uow.evaluations.list_release_gates(workspace.id) == []


class TestDeletesOverSql:
    """Every delete the app offers removes its rows, children first, over SQL."""

    async def test_each_delete_removes_exactly_its_rows(self, sql_stack) -> None:
        from datetime import UTC, datetime

        from lab_assistant.application.scopes import ScopeService
        from lab_assistant.domain.observation import (
            DiscoveryRun,
            Predicate,
            SourceAssertion,
            SourceObservation,
            SourceRequest,
        )
        from lab_assistant.domain.scope import ConfigKind, ConfigVersion, ConnectorPlanEntry

        context, accounts = sql_stack["context"], sql_stack["accounts"]
        owner = await accounts.create_user(email="d@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="delete-lab", display_name="Delete Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        scope = await ScopeService(context).create_version(
            admin,
            scope_id=None,
            name="Scope",
            scientific_objective="o",
            connector_plan=[
                ConnectorPlanEntry(connector_id="pubmed", connector_version="1.0.0", query="q")
            ],
            intended_uses=("y",),
            concepts=("z",),
        )
        now = datetime(2026, 11, 22, 9, tzinfo=UTC)
        ws = workspace.id
        run_id, request_id, observation_id = (context.id_factory() for _ in range(3))
        task_id, config_id, eval_id = (context.id_factory() for _ in range(3))
        async with context.uow_factory() as uow:
            await uow.observations.add_run(
                DiscoveryRun(
                    id=run_id,
                    workspace_id=ws,
                    scope_version_id=scope.id,
                    run_fingerprint="f",
                    requested_by=owner.id,
                    started_at=now,
                )
            )
            await uow.observations.add_request(
                SourceRequest(
                    id=request_id,
                    workspace_id=ws,
                    discovery_run_id=run_id,
                    connector_id="pubmed",
                    connector_version="1.0.0",
                    request_fingerprint="rf",
                    method="GET",
                    canonical_url_redacted="https://example.invalid/q",
                    requested_at=now,
                )
            )
            await uow.observations.add_observation(
                SourceObservation(
                    id=observation_id,
                    workspace_id=ws,
                    source_request_id=request_id,
                    source_record_locator="doi:10.1/x",
                    parser_id="p",
                    parser_version="1",
                    schema_version="1",
                    observed_at=now,
                )
            )
            await uow.observations.add_assertions(
                [
                    SourceAssertion(
                        id=context.id_factory(),
                        workspace_id=ws,
                        source_observation_id=observation_id,
                        subject_hint="doi:10.1/x",
                        predicate=Predicate.TITLE,
                        value_json="A title",
                    )
                ]
            )
            await uow.tasks.add(
                Task(
                    id=task_id,
                    workspace_id=ws,
                    stage=StageName.DISCOVERY,
                    state=TaskState.DEAD_LETTER,
                    input_fingerprint="tf",
                    run_id=run_id,
                    created_at=now,
                )
            )
            await uow.configs.add_config_version(
                ConfigVersion(
                    id=config_id,
                    workspace_id=ws,
                    kind=ConfigKind.EXTRACTION_TEMPLATE,
                    logical_id="t",
                    semantic_version="1",
                    payload={"a": 1},
                    created_at=now,
                )
            )
            await uow.evaluations.add_benchmark(
                {
                    "id": context.id_factory(),
                    "workspace_id": ws,
                    "benchmark_id": "b",
                    "version": "1",
                    "kind": "retrieval",
                    "checksum": "c",
                    "sensitivity": "internal",
                    "definition": {},
                }
            )
            await uow.evaluations.add_run(
                {
                    "id": eval_id,
                    "workspace_id": ws,
                    "benchmark": "b@1",
                    "benchmark_checksum": "c",
                    "status": "failed",
                    "created_at": now,
                }
            )
            await uow.commit()

        async with context.uow_factory() as uow:
            assert [task.id for task in await uow.tasks.list_run_tasks(ws, run_id)] == [task_id]
            await uow.tasks.delete_tasks(ws, [task_id])
            await uow.observations.delete_run(ws, run_id)
            await uow.configs.delete_config_version(ws, config_id)
            await uow.evaluations.delete_run(ws, eval_id)
            await uow.evaluations.delete_benchmark(ws, "b", "1")
            await uow.commit()

        async with context.uow_factory() as uow:
            assert await uow.observations.get_run(ws, run_id) is None
            assert await uow.observations.list_requests(ws, run_id) == []
            assert await uow.observations.list_observations(ws) == []
            assert await uow.observations.list_assertions(ws) == []
            assert await uow.tasks.get(ws, task_id) is None
            assert await uow.configs.list_config_versions(ws) == []
            assert await uow.evaluations.get_run(ws, eval_id) is None
            assert await uow.evaluations.get_benchmark(ws, "b", "1") is None
            # The scope the run belonged to is untouched.
            assert await uow.configs.get_scope_version(ws, scope.id) is not None

    async def test_a_revoked_invitation_and_a_draft_release_are_deleted(self, sql_stack) -> None:
        from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService

        context, accounts = sql_stack["context"], sql_stack["accounts"]
        owner = await accounts.create_user(email="i@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="invite-lab", display_name="Invite Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        invitation, _ = await accounts.invite(admin, email="n@lab.invalid", role=Role.RESEARCHER)
        await accounts.revoke_invitation(admin, invitation_id=invitation.id)
        await accounts.delete_invitation(admin, invitation_id=invitation.id)

        releases = ReleaseService(context)
        draft, _, _ = await releases.draft(
            admin, ReleaseDraftRequest(name="corpus", semantic_version="0.1.0")
        )
        await releases.delete_draft(admin, release_id=draft.id)

        async with context.uow_factory() as uow:
            assert await uow.workspaces.list_invitations(workspace.id) == []
            assert await uow.releases.get(workspace.id, draft.id) is None
            assert await uow.releases.list_items(workspace.id, draft.id) == []

    async def test_a_temporary_password_is_one_sign_in_and_then_the_persons_own(
        self, sql_stack
    ) -> None:
        from lab_assistant.application.accounts import PasswordChangeRequired
        from lab_assistant.domain.workspace import InvitationKind

        accounts = sql_stack["accounts"]
        owner = await accounts.create_user(email="t@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="temporary-lab", display_name="Temporary Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        _invitation, temporary = await accounts.invite(
            admin,
            email="p@lab.invalid",
            role=Role.RESEARCHER,
            kind=InvitationKind.TEMPORARY_PASSWORD,
        )
        listed = await accounts.list_invitations(admin)
        assert [item.kind for item in listed] == [InvitationKind.TEMPORARY_PASSWORD]

        with pytest.raises(PasswordChangeRequired):
            await accounts.authenticate(email="p@lab.invalid", password=temporary)
        user, membership = await accounts.set_first_password(
            email="p@lab.invalid", temporary_password=temporary, password="their-own-passphrase"
        )
        assert membership.workspace_id == workspace.id
        signed_in = await accounts.authenticate(
            email="p@lab.invalid", password="their-own-passphrase"
        )
        assert signed_in.id == user.id


class TestAcquisitionQueueSearchesPapersLikeCandidates:
    """The queue's search takes the candidate list's paper fields, on SQL as in memory."""

    async def test_paper_fields_narrow_the_plans(self, sql_stack) -> None:
        from lab_assistant.domain.assets import AcquisitionPlan
        from lab_assistant.domain.identity import normalise_title
        from lab_assistant.domain.review import CandidateFilter, WorkSummary

        context, accounts = sql_stack["context"], sql_stack["accounts"]
        owner = await accounts.create_user(email="q@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="queue-lab", display_name="Queue Lab", owner_user_id=owner.id
        )
        ws = workspace.id
        papers = [
            ("Lipid yield in Yarrowia", 2021, "Metabolic Engineering", "assisted"),
            ("Lipid droplets, a review", 2015, "Trends in Biotechnology", "assisted"),
            ("Sterol pathways in yeast", 2022, "Metabolic Engineering", "fetched"),
        ]
        async with context.uow_factory() as uow:
            for title, year, journal, status in papers:
                work = Work(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_kind=WorkKind.ARTICLE,
                    relevance=RelevanceState.INCLUDED,
                    created_at=datetime.now(UTC),
                )
                await uow.works.add(work)
                await uow.works.upsert_summaries(
                    [
                        WorkSummary(
                            work_id=work.id,
                            workspace_id=ws,
                            title=title,
                            title_search=normalise_title(title),
                            year=year,
                            journal=journal,
                        )
                    ]
                )
                await uow.assets.add_plan(
                    AcquisitionPlan(
                        id=context.new_id(),
                        workspace_id=ws,
                        work_id=work.id,
                        status=status,
                        created_at=datetime.now(UTC),
                    )
                )
            await uow.commit()

        async def found(**kwargs) -> int:
            async with context.uow_factory() as uow:
                listed = await uow.assets.list_plans(ws, limit=50, **kwargs)
                counted = await uow.assets.count_plans(ws, **kwargs)
            assert len(listed) == counted
            return counted

        assert await found(papers=CandidateFilter()) == 3  # an empty filter keeps every plan
        assert await found(papers=CandidateFilter(title_words="lipid")) == 2
        assert await found(papers=CandidateFilter(title_words="lipid", exclude_words="review")) == 1
        assert await found(papers=CandidateFilter(year_from=2020)) == 2
        assert await found(papers=CandidateFilter(journal="metabolic")) == 2
        # Together with the queue's own fields: status and paper both hold.
        assert await found(status="fetched", papers=CandidateFilter(journal="metabolic")) == 1


class TestAcquisitionExportOverSql:
    """The queue's CSV: every page of a search, with each paper's links."""

    async def test_every_page_of_a_search_with_its_links(self, sql_stack, monkeypatch) -> None:
        import csv
        import io
        import zipfile

        from lab_assistant.application import acquisition_export
        from lab_assistant.application.acquisition_export import (
            AcquisitionExportService,
            rows_csv,
            rows_zip,
        )
        from lab_assistant.contracts.errors import AcquisitionOutcome
        from lab_assistant.domain.assets import (
            AcquisitionAttempt,
            AcquisitionPlan,
            AcquisitionRoute,
        )
        from lab_assistant.domain.review import WorkSummary

        # Three reads of two plans each, so the export pages as a large queue does.
        monkeypatch.setattr(acquisition_export, "_BATCH", 2)
        context, accounts = sql_stack["context"], sql_stack["accounts"]
        owner = await accounts.create_user(email="x@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="export-lab", display_name="Export Lab", owner_user_id=owner.id
        )
        ws = workspace.id
        plans = []
        async with context.uow_factory() as uow:
            for index in range(5):
                work = Work(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_kind=WorkKind.ARTICLE,
                    relevance=RelevanceState.INCLUDED,
                    created_at=datetime.now(UTC),
                )
                await uow.works.add(work)
                await uow.works.upsert_summaries(
                    [WorkSummary(work_id=work.id, workspace_id=ws, title=f"=Paper {index}")]
                )
                await uow.works.add_identifier(
                    WorkIdentifier(
                        id=context.new_id(),
                        workspace_id=ws,
                        work_id=work.id,
                        scheme=IdentifierScheme.DOI,
                        value_normalized=f"10.1000/p{index}",
                        value_display=f"10.1000/P{index}",
                    )
                )
                plan = AcquisitionPlan(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_id=work.id,
                    status="assisted" if index % 2 == 0 else "fetched",
                    created_at=datetime.now(UTC),
                )
                await uow.assets.add_plan(plan)
                plans.append(plan)
            await uow.works.add_identifier(
                WorkIdentifier(
                    id=context.new_id(),
                    workspace_id=ws,
                    work_id=plans[0].work_id,
                    scheme=IdentifierScheme.PMCID,
                    value_normalized="pmc123",
                    value_display="PMC123",
                )
            )
            for number, outcome in enumerate(
                (AcquisitionOutcome.NOT_FOUND, AcquisitionOutcome.ENTITLEMENT_REQUIRED), start=1
            ):
                await uow.assets.add_attempt(
                    AcquisitionAttempt(
                        id=context.new_id(),
                        workspace_id=ws,
                        acquisition_plan_id=plans[0].id,
                        manifestation_id=None,
                        connector_id="unpaywall",
                        route=AcquisitionRoute.OPEN_REPOSITORY,
                        attempt_number=number,
                        outcome=outcome,
                        requested_at=datetime.now(UTC),
                        source_url_redacted=f"https://publisher.example/p0-{number}.pdf",
                    )
                )
            await uow.commit()

        actor = make_actor(user_id=owner.id, workspace_id=ws, role=Role.ADMIN)
        service = AcquisitionExportService(context)
        every = await service.rows(actor)
        assert [row["plan_id"] for row in every] == sorted(str(plan.id) for plan in plans)

        assisted = await service.rows(actor, status="assisted")
        assert len(assisted) == 3
        first = next(row for row in assisted if row["plan_id"] == str(plans[0].id))
        assert first["doi"] == "10.1000/P0" and first["pmcid"] == "PMC123"
        assert first["url"] == first["doi_url"] == "https://doi.org/10.1000/P0"
        assert first["pmc_url"] == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/"
        # The latest attempt: its answer, and where it looked.
        assert first["last_outcome"] == "entitlement_required"
        assert first["last_tried_url"] == "https://publisher.example/p0-2.pdf"
        others = [row for row in assisted if row is not first]
        assert {row["last_outcome"] for row in others} == {"never tried"}

        found = await service.rows(actor, words=("10.1000/p3",))
        assert [row["work_id"] for row in found] == [str(plans[3].work_id)]

        # A title that starts like a formula is escaped in the file.
        text = rows_csv(every).decode("utf-8-sig")
        parsed = list(csv.DictReader(io.StringIO(text)))
        assert len(parsed) == 5 and parsed[0]["title"].startswith("'=Paper")

        archive = zipfile.ZipFile(io.BytesIO(rows_zip(every, "queue", per_file=2)))
        assert archive.namelist() == ["queue-001.csv", "queue-002.csv", "queue-003.csv"]
        sizes = [
            len(list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig")))))
            for name in archive.namelist()
        ]
        assert sizes == [2, 2, 1]


class TestReconciliationOverSql:
    """Reconciling a run large enough to need several batches.

    Each paper is seen by two or three sources. For a third of them the first two
    records share nothing (one has only a PMID, the other only a DOI) and a third
    record carries both: two works with no identifier in common, which the rules
    send to a person as a conflict rather than merging.
    """

    PAPERS = 240
    TITLE_ONLY = 30

    async def _run(self, sql_stack) -> dict:
        from lab_assistant.application.scopes import ScopeService
        from lab_assistant.domain.observation import (
            DiscoveryRun,
            Predicate,
            SourceAssertion,
            SourceObservation,
            SourceRequest,
        )
        from lab_assistant.domain.scope import ConnectorPlanEntry

        context, accounts = sql_stack["context"], sql_stack["accounts"]
        owner = await accounts.create_user(email="r@lab.invalid", password="a-long-passphrase")
        workspace, _ = await accounts.create_workspace(
            slug="recon-lab", display_name="Recon Lab", owner_user_id=owner.id
        )
        admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
        scope = await ScopeService(context).create_version(
            admin,
            scope_id=None,
            name="Scope",
            scientific_objective="o",
            connector_plan=[
                ConnectorPlanEntry(connector_id="pubmed", connector_version="1.0.0", query="q")
            ],
            intended_uses=("y",),
            concepts=("yarrowia",),
        )
        now = datetime(2026, 11, 22, 9, tzinfo=UTC)
        ws = workspace.id
        run_id, request_id = context.id_factory(), context.id_factory()

        def records():
            for k in range(self.PAPERS):
                doi = {"scheme": "doi", "value": f"10.1000/paper.{k}"}
                pmid = {"scheme": "pmid", "value": str(100_000 + k)}
                title = f"Yarrowia study number {k} of lipid pathway variant {k * 7}"
                if k % 3 == 0:
                    yield f"a-pubmed-{k:05}", [pmid], title
                    yield f"b-crossref-{k:05}", [doi], title
                    yield f"c-europepmc-{k:05}", [pmid, doi], title
                else:
                    yield f"a-pubmed-{k:05}", [pmid, doi], title
                    yield f"b-crossref-{k:05}", [doi], title
            for k in range(self.TITLE_ONLY):
                yield f"d-datacite-{k:05}", [], f"A dataset with no identifier, item {k}"

        async with context.uow_factory() as uow:
            await uow.observations.add_run(
                DiscoveryRun(
                    id=run_id,
                    workspace_id=ws,
                    scope_version_id=scope.id,
                    run_fingerprint="f",
                    requested_by=owner.id,
                    started_at=now,
                )
            )
            await uow.observations.add_request(
                SourceRequest(
                    id=request_id,
                    workspace_id=ws,
                    discovery_run_id=run_id,
                    connector_id="pubmed",
                    connector_version="1.0.0",
                    request_fingerprint="rf",
                    method="GET",
                    canonical_url_redacted="https://example.invalid/q",
                    requested_at=now,
                )
            )
            count = 0
            for hint, identifiers, title in records():
                observation_id = context.id_factory()
                await uow.observations.add_observation(
                    SourceObservation(
                        id=observation_id,
                        workspace_id=ws,
                        source_request_id=request_id,
                        source_record_locator=hint,
                        parser_id="p",
                        parser_version="1",
                        schema_version="1",
                        observed_at=now,
                    )
                )
                values = [(Predicate.TITLE, title), (Predicate.YEAR, 2024)]
                values += [(Predicate.IDENTIFIER, identifier) for identifier in identifiers]
                await uow.observations.add_assertions(
                    [
                        SourceAssertion(
                            id=context.id_factory(),
                            workspace_id=ws,
                            source_observation_id=observation_id,
                            subject_hint=hint,
                            predicate=predicate,
                            value_json=value,
                        )
                        for predicate, value in values
                    ]
                )
                count += 1
            await uow.commit()
        return {"workspace_id": ws, "run_id": run_id, "owner": owner.id, "records": count}

    @staticmethod
    def _command(run: dict):
        from lab_assistant.contracts.stage import StageCommand

        return StageCommand(
            stage=StageName.RECONCILIATION,
            workspace_id=run["workspace_id"],
            actor_id=run["owner"],
            inputs={"discovery_run_id": str(run["run_id"])},
            run_id=run["run_id"],
        )

    @staticmethod
    async def _state(context, workspace_id) -> dict:
        async with context.uow_factory() as uow:
            works = await uow.works.list_works(workspace_id, limit=100_000)
            identifiers = await uow.works.list_all_identifiers(workspace_id, limit=100_000)
            decisions = await uow.works.list_decisions(workspace_id)
        active = [w for w in works if w.is_active]
        active_ids = {w.id for w in active}
        return {
            "active": len(active),
            "merged": len(works) - len(active),
            "orphaned_identifiers": sum(1 for i in identifiers if i.work_id not in active_ids),
            "identifiers": len(identifiers),
            "proposals": [
                d for d in decisions if (d.after or {}).get("proposal") == "duplicate_review"
            ],
        }

    async def test_records_resolve_to_works_across_batches(self, sql_stack, monkeypatch) -> None:
        from lab_assistant.application import reconciliation

        monkeypatch.setattr(reconciliation, "RECONCILIATION_BATCH_SIZE", 100)
        run = await self._run(sql_stack)
        handler = sql_stack["handlers"].get(StageName.RECONCILIATION)
        result = await handler.execute(self._command(run))

        bridged = len(range(0, self.PAPERS, 3))
        assert result.metrics["automatic_merges"] == 0
        # The bridging record finds both works (one conflict) and then cannot
        # attach the second work's identifier (another).
        assert result.metrics["conflicts"] == 2 * bridged
        assert result.metrics["works_created"] == self.PAPERS + bridged + self.TITLE_ONLY
        state = await self._state(sql_stack["context"], run["workspace_id"])
        assert state["active"] == self.PAPERS + bridged + self.TITLE_ONLY
        assert state["merged"] == 0
        assert state["orphaned_identifiers"] == 0
        assert state["identifiers"] == 2 * self.PAPERS
        # Each conflicting pair shares a title, so it is queued for review once;
        # no work is proposed as a duplicate of itself.
        assert len(state["proposals"]) == bridged
        assert all(len(set(p.subject_work_ids)) == 2 for p in state["proposals"])

    async def test_reconciliation_builds_the_review_index_and_backfill_rebuilds_it(
        self, sql_stack, monkeypatch
    ) -> None:
        from sqlalchemy import delete, update

        from lab_assistant.adapters.db import tables as t
        from lab_assistant.application import reconciliation
        from lab_assistant.application.bulk_review import BulkReviewService
        from lab_assistant.domain.review import CandidateFilter

        monkeypatch.setattr(reconciliation, "RECONCILIATION_BATCH_SIZE", 100)
        run = await self._run(sql_stack)
        context, ws = sql_stack["context"], run["workspace_id"]
        await sql_stack["handlers"].get(StageName.RECONCILIATION).execute(self._command(run))
        bridged = len(range(0, self.PAPERS, 3))

        async def index() -> tuple[int, int, int]:
            async with context.uow_factory() as uow:
                facets = await uow.works.summary_facets(ws)
                queued = await uow.works.count_duplicate_candidates(ws, status="open")
                yarrowia = await uow.works.count_filtered(
                    ws, CandidateFilter(title_words=["yarrowia"])
                )
            return facets["summarised"], queued, yarrowia

        works = self.PAPERS + bridged + self.TITLE_ONLY
        built = await index()
        assert built == (works, bridged, self.PAPERS + bridged)
        async with context.uow_factory() as uow:
            facets = await uow.works.summary_facets(ws)
            decisions = await uow.works.list_decisions(ws)
        # Every record was seen by the one connector the fixture's request names.
        assert facets["sources"] == {"pubmed": works}
        # One relevance decision per work that changed, not one per record: a work
        # seen by three sources used to be "decided" three times over.
        relevance = [d for d in decisions if d.decision_type.value == "set_relevance"]
        assert len(relevance) == len({d.subject_work_ids[0] for d in relevance})
        assert len(relevance) < run["records"]

        # A database from before the index: summaries and queue rows gone.
        async with sql_stack["engine"].begin() as connection:
            await connection.execute(delete(t.duplicate_candidates))
            await connection.execute(delete(t.work_summaries))
        assert await index() == (0, 0, 0)
        done = await BulkReviewService(context).backfill(ws)
        # Reconciliation recorded its step, so the history needs no backfill.
        assert done == {"summarised": works, "queued": bridged, "history": 0}
        assert await index() == built
        assert await BulkReviewService(context).backfill(ws) == {
            "summarised": 0,
            "queued": 0,
            "history": 0,
        }

        # The deploy runs it as `lab db backfill-review`, over every workspace.
        import argparse
        from types import SimpleNamespace

        from lab_assistant.cli.main import _backfill_review

        async with sql_stack["engine"].begin() as connection:
            await connection.execute(delete(t.duplicate_candidates))
            await connection.execute(delete(t.work_summaries))
        container = SimpleNamespace(context=context, shutdown=[])
        code = await _backfill_review(
            argparse.Namespace(workspace=None), context.settings, container=container
        )
        assert code == 0
        assert await index() == built

        # A summary written before the rules read summaries is rebuilt.
        async with sql_stack["engine"].begin() as connection:
            await connection.execute(update(t.work_summaries).values(relevance_text=None))
        done = await BulkReviewService(context).backfill(ws)
        assert done["summarised"] == works
        async with context.uow_factory() as uow:
            page = await uow.works.list_works(ws, limit=50)
            summaries = await uow.works.get_summaries(ws, [w.id for w in page])
        assert all(s.relevance_text is not None for s in summaries.values())
        assert any("yarrowia" in (s.relevance_text or "") for s in summaries.values())

    async def test_a_retry_after_a_crash_resumes_without_duplicating(
        self, sql_stack, monkeypatch
    ) -> None:
        from lab_assistant.application import reconciliation

        monkeypatch.setattr(reconciliation, "RECONCILIATION_BATCH_SIZE", 100)
        run = await self._run(sql_stack)
        handler = sql_stack["handlers"].get(StageName.RECONCILIATION)
        real = reconciliation.ReconciliationHandler._reconcile_record
        calls = 0

        async def crash_in_the_third_batch(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 250:
                raise RuntimeError("worker stopped")
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(
            reconciliation.ReconciliationHandler, "_reconcile_record", crash_in_the_third_batch
        )
        with pytest.raises(RuntimeError, match="worker stopped"):
            await handler.execute(self._command(run))
        partial = await self._state(sql_stack["context"], run["workspace_id"])
        # Two batches committed: the candidates so far are visible, not lost.
        assert 0 < partial["active"] < self.PAPERS
        bridged = len(range(0, self.PAPERS, 3))

        monkeypatch.setattr(reconciliation.ReconciliationHandler, "_reconcile_record", real)
        await handler.execute(self._command(run))
        state = await self._state(sql_stack["context"], run["workspace_id"])
        # Exactly what one uninterrupted pass produces: the records with no
        # identifier at all kept the works the first attempt gave them.
        assert state["active"] == self.PAPERS + bridged + self.TITLE_ONLY
        assert state["orphaned_identifiers"] == 0

        # And a second full pass over the same run changes nothing.
        again = await handler.execute(self._command(run))
        assert again.metrics["works_created"] == 0
        assert await self._state(sql_stack["context"], run["workspace_id"]) == state
