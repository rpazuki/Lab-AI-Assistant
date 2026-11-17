"""FastAPI application factory.

Errors leave as problem details, so a client branches on ``error_class`` instead
of parsing prose. Requests carry a correlation id into logs. A production
deployment refuses to start with development defaults still in place — a check
that exists because the predecessor's plan warned about exactly that and nothing
enforced it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lab_assistant import __version__
from lab_assistant.config import Settings, load_settings
from lab_assistant.contracts.errors import LabAssistantError, PolicyDenied, ProblemDetails
from lab_assistant.domain.workspace import Capability
from lab_assistant_api.deps import ApiContainer, CurrentActor, build_container
from lab_assistant_api.routes import router

__all__ = ["create_app"]

logger = structlog.get_logger("lab_assistant_api")


def create_app(
    *, settings: Settings | None = None, container: ApiContainer | None = None
) -> FastAPI:
    resolved_settings = settings or load_settings()[0]
    if resolved_settings.is_production:
        blockers = resolved_settings.production_blockers()
        if blockers:
            raise RuntimeError(
                "refusing to start in production with development settings: " + "; ".join(blockers)
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "container", None) is None:
            app.state.container = build_container(resolved_settings)
        logger.info(
            "api.started",
            version=__version__,
            environment=resolved_settings.env.value,
            llm_provider=resolved_settings.llm_provider,
            embedding_profile=resolved_settings.embedding_profile,
        )
        try:
            yield
        finally:
            for hook in app.state.container.shutdown:
                await hook()

    app = FastAPI(
        title="Lab AI Assistant",
        version=__version__,
        description=(
            "Internal research-evidence platform. No public query endpoint: every "
            "route requires an authenticated workspace member."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Workspace", "X-Request-Id"],
    )

    @app.middleware("http")
    async def correlate(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(LabAssistantError)
    async def domain_error_handler(request: Request, exc: LabAssistantError) -> JSONResponse:
        problem = exc.to_problem(instance=str(request.url.path))
        logger.warning(
            "api.domain_error",
            error_class=exc.error_class.value,
            status=exc.http_status,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=problem.model_dump(mode="json"),
            media_type="application/problem+json",
        )

    @app.exception_handler(AssertionError)
    async def isolation_error_handler(request: Request, exc: AssertionError) -> JSONResponse:
        """A workspace-scope assertion is a bug, and it must never leak detail."""
        logger.error("api.workspace_scope_violation", path=request.url.path)
        problem = ProblemDetails(
            type="https://lab-ai-assistant.invalid/problems/not_found",
            title="not found",
            status=404,
            detail="not found",
            error_class="not_found",  # type: ignore[arg-type]
            instance=str(request.url.path),
        )
        return JSONResponse(
            status_code=404,
            content=problem.model_dump(mode="json"),
            media_type="application/problem+json",
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, Any]:
        """Readiness: can we reach the control plane and the object store?"""
        container = request.app.state.container
        checks: dict[str, bool] = {}
        try:
            async with container.context.uow_factory() as uow:
                await uow.users.list_all(limit=1)
            checks["database"] = True
        except Exception:  # pragma: no cover - reported, not raised
            checks["database"] = False
        store = container.context.object_store
        try:
            ping = getattr(store, "ping", None)
            if ping is not None:
                # A remote store answers a cheap list; writing an object per probe
                # would put a readiness artefact in the bucket every few seconds.
                await ping()
                checks["object_store"] = True
            else:
                stored = await store.put(b"readiness", workspace_id=None, object_class="readiness")
                checks["object_store"] = await store.verify(stored.id)
        except Exception:  # pragma: no cover - reported, not raised
            checks["object_store"] = False
        status = "ok" if all(checks.values()) else "degraded"
        return {"status": status, "checks": checks}

    @app.get("/api/v1/config")
    async def effective_config(request: Request, actor: CurrentActor) -> dict[str, Any]:
        """Redacted effective configuration, for operators.

        Secret values are never printed — only a digest, so two deployments can be
        compared without disclosing anything. Redaction is not on its own a reason
        to publish it: deployment topology, provider choices, and which staged
        controls are off are exactly what someone probing the system wants. The
        README says every route requires an authenticated member; this route
        additionally requires the operations capability.
        """
        if not actor.has(Capability.VIEW_OPERATIONS):
            raise PolicyDenied("effective configuration is limited to operators")
        container = request.app.state.container
        return {
            "environment": container.settings.env.value,
            "config": container.settings.redacted(),
            "read_by": str(actor.user_id),
            "staged_controls": {
                "row_level_security_enabled": container.settings.row_level_security_enabled,
                "malware_scan_required_for_uploads": (
                    container.settings.malware_scan_required_for_uploads
                ),
                "malware_scan_required_for_open_access": (
                    container.settings.malware_scan_required_for_open_access
                ),
                "double_review_enabled": container.settings.double_review_enabled,
                "tracing_enabled": container.settings.tracing_enabled,
            },
        }

    # An injected container is attached immediately, so a caller that drives the
    # app without running lifespan (an ASGI transport in a test) still has it.
    app.state.container = container
    app.include_router(router)
    return app


app = create_app  # uvicorn factory target: `uvicorn lab_assistant_api.main:app --factory`
