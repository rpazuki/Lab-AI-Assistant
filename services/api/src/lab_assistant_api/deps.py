"""Composition root and shared FastAPI dependencies.

The container is built once at startup from settings and stored on
``app.state``. Tests replace it wholesale with the deterministic harness's
context, so no route needs a monkeypatch of anything it awaits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from lab_assistant.application.accounts import AccountService
from lab_assistant.application.context import ActorContext, ServiceContext, resolve_actor
from lab_assistant.application.tasks import HandlerRegistry, TaskOrchestrator
from lab_assistant.config import Settings
from lab_assistant.contracts.errors import NotFound, PolicyDenied
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant_api.security import AccessToken, TokenError, decode_token

__all__ = [
    "ApiContainer",
    "CurrentActor",
    "CurrentToken",
    "build_container",
    "container_of",
    "get_actor",
    "get_container",
    "get_token",
    "requires",
]


@dataclass(frozen=True, slots=True)
class ApiContainer:
    """Everything the API needs, assembled once."""

    settings: Settings
    context: ServiceContext
    accounts: AccountService
    handlers: HandlerRegistry
    orchestrator: TaskOrchestrator
    #: Optional shutdown hooks (database engine disposal, and so on).
    shutdown: tuple[Callable[[], Awaitable[None]], ...] = ()
    #: Reads the migration revision the database is stamped at, for the
    #: readiness probe. ``None`` on the in-memory profile, where there are no
    #: migrations and the check reports itself as not performed.
    schema_revision: Callable[[], Awaitable[str | None]] | None = None


def build_container(settings: Settings) -> ApiContainer:
    """Assemble the production container from settings.

    Every adapter choice is a setting, so a deployment changes behaviour by
    configuration rather than by code, and the offline profile is a real profile
    rather than a test-only path.
    """
    from lab_assistant.adapters.connectors.registry import live_connector_registry
    from lab_assistant.adapters.memory.repositories import MemoryDatabase, memory_uow_factory
    from lab_assistant.adapters.memory.search import MemorySearchIndex
    from lab_assistant.adapters.models.fake import FakeLLMProvider, HashingEmbeddingProvider
    from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
    from lab_assistant.adapters.parsers.registry import default_parser_registry
    from lab_assistant.application import build_handler_registry
    from lab_assistant.application.tasks import HandlerRegistry
    from lab_assistant.contracts.runtime import system_clock, uuid_factory
    from lab_assistant.domain.installation import (
        InstallationCapability,
        InstanceCapabilities,
    )
    from lab_assistant.policy.engine import PolicyEngine
    from lab_assistant.policy.model_transfer import ModelTransferPolicy
    from lab_assistant.policy.rights import default_open_access_policy

    shutdown: list[Callable[[], Awaitable[None]]] = []

    schema_revision: Callable[[], Awaitable[str | None]] | None = None
    if settings.database_url.startswith("memory"):
        uow_factory = memory_uow_factory(MemoryDatabase())
        search = MemorySearchIndex()
    else:
        from lab_assistant.adapters.db.engine import create_engine_and_factory
        from lab_assistant.adapters.db.migrations import schema_revision_reader

        engine, uow_factory, search = create_engine_and_factory(settings)
        shutdown.append(engine.dispose)
        # So `/ready` can refuse traffic when this build's migrations have not
        # run — the most common way a deploy goes wrong, and the one thing the
        # previous probe did not look at.
        schema_revision = schema_revision_reader(engine)

    if settings.object_store_kind == "s3":
        from lab_assistant.adapters.object_store.s3 import S3ObjectStore, S3StoreConfig

        # Refuses to construct when the bucket, the region, or any resolvable
        # credential is missing: better at startup than during an acquisition
        # run at 2am. Credentials may come from the SDK chain rather than
        # settings, so a role-based deployment configures no keys at all.
        object_store = S3ObjectStore(S3StoreConfig.from_settings(settings))
        shutdown.append(object_store.aclose)
    elif settings.object_store_kind == "gcs":
        from lab_assistant.adapters.object_store.gcs import GcsObjectStore, GcsStoreConfig

        # Same refusal at construction, for the same reason: a bucket that is
        # not there is a startup failure, not a 2am one (ADR-0024).
        object_store = GcsObjectStore(GcsStoreConfig.from_settings(settings))
        shutdown.append(object_store.aclose)
    elif settings.object_store_kind == "filesystem":
        object_store = FilesystemObjectStore(
            settings.object_store_root,
            max_object_bytes=settings.object_store_max_object_bytes or None,
        )
    else:
        # Never read as a filesystem path. Only tested providers are supported,
        # and an unknown value is a configuration error rather than a default.
        raise ValueError(
            f"unknown LAB_OBJECT_STORE_KIND {settings.object_store_kind!r}; "
            "expected 'filesystem', 's3' or 'gcs'"
        )

    if settings.llm_provider == "anthropic" and settings.llm_api_key:
        from lab_assistant.adapters.models.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(api_key=settings.llm_api_key, model=settings.llm_model)
    else:
        llm = FakeLLMProvider()

    if settings.embedding_profile == "hashing-v1":
        embedder = HashingEmbeddingProvider()
    else:
        from lab_assistant.adapters.models.embeddings import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder(
            profile_id=settings.embedding_profile,
            cache_folder=settings.embedding_model_cache,
            batch_size=settings.embedding_batch_size,
        )

    from lab_assistant.adapters.models.extractor import ModelExtractor

    # What this deployment is for, which decides what gets built at all. A
    # reader is not a publisher with some routes switched off: it has no
    # connectors, no parsers, no extractor and no scanner, so the acquisition
    # and processing paths have nothing to call even if one were reached.
    #
    # That is the strongest of the four enforcement layers, because it removes
    # the capability rather than guarding it — and it is the reason a reader
    # needs no source API keys and no ClamAV socket to serve a corpus somebody
    # else approved (audit F07).
    installation = InstanceCapabilities(role=settings.installation_role)
    curates = installation.has(InstallationCapability.ACQUIRE_SOURCES)
    processes = installation.has(InstallationCapability.PROCESS_CONTENT)

    # Scanning is required by default for uploads. A container without a scanner
    # would accept an upload, submit a parse task, and leave it stuck reporting
    # `scanner_unavailable` — which reads as a bug rather than as the control it is.
    # A reader accepts no uploads, so it needs none.
    needs_scanner = (
        settings.malware_scan_required_for_uploads or settings.malware_scan_required_for_open_access
    )
    if curates and needs_scanner:
        from lab_assistant.adapters.security import ClamAvScanner

        scanner = ClamAvScanner()
    else:
        scanner = None

    context = ServiceContext(
        uow_factory=uow_factory,
        object_store=object_store,
        clock=system_clock,
        id_factory=uuid_factory,
        policy_engine=PolicyEngine(),
        rights_policy=default_open_access_policy(
            replicate_open_content=settings.rights_replicate_open_content
        ),
        model_transfer_policy=ModelTransferPolicy(
            approved_providers=("anthropic",) if settings.external_model_enabled else ()
        ),
        settings=settings,
        search_index=search,
        connectors=live_connector_registry(settings) if curates else None,
        parsers=default_parser_registry() if processes else None,
        llm_provider=llm,
        embedding_provider=embedder,
        # The extractor proposes claims, which is processing. Retrieval and chat
        # use `llm_provider` directly and are unaffected, so a reader still
        # answers questions about the corpus it serves.
        extractor=ModelExtractor(provider=llm) if processes else None,
        malware_scanner=scanner,
        installation=installation,
    )
    # A reader registers no stage handlers: there is nothing for it to run, and
    # an empty registry is a clearer statement than a populated one guarded at
    # every entry point.
    handlers = build_handler_registry(context) if processes or curates else HandlerRegistry()
    if installation.has(InstallationCapability.PUBLISH_RELEASES):
        # Registered here, not in `build_handler_registry`: the application layer
        # may not import evaluation, which sits above it.
        from lab_assistant.evaluation.service import EvaluationHandler

        handlers.register(EvaluationHandler(context))
    return ApiContainer(
        settings=settings,
        context=context,
        accounts=AccountService(context),
        handlers=handlers,
        orchestrator=TaskOrchestrator(context, handlers),
        shutdown=tuple(shutdown),
        schema_revision=schema_revision,
    )


def requires(capability: InstallationCapability):
    """Declare the installation capability a route needs.

    Used as a route dependency rather than a check inside the handler so that
    it is visible in the route table — which is what lets
    `tests/contract/test_installation_role.py` fail when a new mutating route
    forgets one. A guard every author has to remember is a guard that will be
    missed; a guard a test enumerates is not.

    The refusal is 501, not 403: no grant and no promotion makes a reader into
    the publisher, so a client must not offer the user a permissions dialog.
    """

    async def dependency(container: Annotated[ApiContainer, Depends(container_of)]) -> None:
        container.context.require_installation(capability)

    dependency.installation_capability = capability  # type: ignore[attr-defined]
    return Depends(dependency)


def container_of(request: Request) -> ApiContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("the API container was not initialised")
    return container


def get_container(container: Annotated[ApiContainer, Depends(container_of)]) -> ApiContainer:
    return container


async def get_token(
    request: Request,
    container: Annotated[ApiContainer, Depends(container_of)],
    authorization: Annotated[str | None, Header()] = None,
) -> AccessToken:
    """Read the bearer token, or the same-origin cookie the web tier sets."""
    raw: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    if raw is None:
        raw = request.cookies.get("lab_session")
    if not raw:
        raise TokenError("no session")
    token = decode_token(raw, secret=container.settings.secret_key)

    async with container.context.uow_factory() as uow:
        user = await uow.users.get(token.user_id)
    if user is None or not user.is_active:
        raise TokenError("no session")
    if user.auth_epoch != token.auth_epoch:
        # The epoch moved: this token was revoked by a password change or an
        # incident response.
        raise TokenError("session has been revoked")
    return token


CurrentToken = Annotated[AccessToken, Depends(get_token)]


async def get_actor(
    token: CurrentToken,
    container: Annotated[ApiContainer, Depends(container_of)],
    request: Request,
    x_workspace: Annotated[str | None, Header(alias="X-Workspace")] = None,
) -> ActorContext:
    """Resolve the caller's membership, groups, and grants for one workspace."""
    workspace_id: UUID | None = None
    if x_workspace:
        try:
            workspace_id = UUID(x_workspace)
        except ValueError:
            async with container.context.uow_factory() as uow:
                workspace = await uow.workspaces.get_by_slug(x_workspace.lower())
            if workspace is None:
                raise NotFound(f"workspace {x_workspace!r} not found") from None
            workspace_id = workspace.id
    if workspace_id is None:
        workspace_id = token.workspace_id
    if workspace_id is None:
        async with container.context.uow_factory() as uow:
            workspaces = await uow.workspaces.list_for_user(token.user_id)
        if not workspaces:
            raise PolicyDenied("this account is not a member of any workspace")
        workspace_id = workspaces[0].id

    actor = await resolve_actor(
        container.context,
        user_id=token.user_id,
        workspace_id=workspace_id,
        request_id=request.headers.get("x-request-id"),
    )
    if actor.membership is None:
        raise PolicyDenied("this account is not a member of that workspace")
    return actor


CurrentActor = Annotated[ActorContext, Depends(get_actor)]
