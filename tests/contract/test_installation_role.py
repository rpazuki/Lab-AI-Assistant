"""Every mutating route declares the installation capability it needs.

A guard each author has to remember is a guard that will eventually be missed,
and the way it gets missed is the cheapest one: somebody adds a route next year
and nothing says anything. The audit has a table of exactly this — guards that
were documented and never wired (F18) — so the rule is enumerated here rather
than described in a convention.

The check is on the route table, not on the handler bodies, because that is
where `requires(...)` is visible and where a new route cannot avoid being seen.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from lab_assistant.config import Settings
from lab_assistant.domain.installation import (
    InstallationCapability,
    InstallationRole,
    InstanceCapabilities,
    capabilities_for,
)
from lab_assistant_api.deps import build_container
from lab_assistant_api.main import create_app

#: Verbs that change something. GET and HEAD are reads and need no capability.
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: Routes outside the versioned API: liveness and readiness, which an
#: orchestrator polls before anything is configured.
EXEMPT_PATHS = {"/health", "/ready"}


def _app():
    settings = Settings(database_url="memory://")
    return create_app(settings=settings, container=build_container(settings))


def _all_routes(container) -> list[APIRoute]:
    """Every route, including those inside an included router.

    This FastAPI version keeps an included router nested rather than flattening
    its routes into `app.routes`, so a non-recursive walk finds three routes and
    reports the whole API as compliant.
    """
    found: list[APIRoute] = []
    for route in getattr(container, "routes", []):
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        # An included router is held as a wrapper around the original, which is
        # where its routes actually live.
        nested = getattr(route, "original_router", None) or (
            route if hasattr(route, "routes") else None
        )
        if nested is not None and nested is not container:
            found.extend(_all_routes(nested))
    return found


def _mutating_routes() -> list[APIRoute]:
    return [
        route
        for route in _all_routes(_app())
        if route.methods & MUTATING and route.path not in EXEMPT_PATHS
    ]


def _declared(route: APIRoute) -> InstallationCapability | None:
    for dependency in route.dependant.dependencies:
        capability = getattr(dependency.call, "installation_capability", None)
        if capability is not None:
            return capability
    return None


def test_there_are_mutating_routes_to_check() -> None:
    """A selector that matches nothing passes every assertion below."""
    assert len(_mutating_routes()) > 10


@pytest.mark.parametrize(
    "route",
    _mutating_routes(),
    ids=lambda route: f"{sorted(route.methods)[0]} {route.path}",
)
def test_a_mutating_route_declares_its_capability(route: APIRoute) -> None:
    assert _declared(route) is not None, (
        f"{sorted(route.methods)} {route.path} changes something and declares no "
        "installation capability, so it would run on a reader. Add "
        "`dependencies=[requires(InstallationCapability.X)]` to the decorator. "
        "If it is genuinely a reader feature, say so with SERVE_CORPUS, "
        "RECORD_INTERACTION or PRIVATE_EXPORT — the point is that somebody chose."
    )


class TestTheRoleModel:
    def test_a_publisher_may_do_everything(self) -> None:
        assert capabilities_for(InstallationRole.PUBLISHER) == frozenset(InstallationCapability)

    def test_a_reader_may_not_change_the_corpus(self) -> None:
        reader = capabilities_for(InstallationRole.READER)
        for denied in (
            InstallationCapability.CURATE_CORPUS,
            InstallationCapability.ACQUIRE_SOURCES,
            InstallationCapability.PROCESS_CONTENT,
            InstallationCapability.PUBLISH_RELEASES,
            InstallationCapability.RUN_WORKERS,
        ):
            assert denied not in reader

    def test_a_reader_may_still_serve_and_record(self) -> None:
        """Not read-only, and that distinction is the whole design.

        A reader writes logins, rate buckets, audit, chat sessions, messages and
        feedback. Blocking writes — or blocking POST, when search is itself a
        POST — would produce an installation that cannot serve anybody (F08).
        """
        reader = capabilities_for(InstallationRole.READER)
        assert InstallationCapability.SERVE_CORPUS in reader
        assert InstallationCapability.RECORD_INTERACTION in reader
        assert InstallationCapability.PRIVATE_EXPORT in reader

    def test_a_new_capability_is_denied_to_readers_by_default(self) -> None:
        """The reader set is an allow-list, so an operation added later is
        denied until somebody decides otherwise — the safe direction."""
        from lab_assistant.domain.installation import READER_CAPABILITIES

        assert frozenset(InstallationCapability) > READER_CAPABILITIES

    def test_the_default_role_keeps_every_capability(self) -> None:
        """An installation that predates the setting must not quietly lose one."""
        assert Settings().installation_role is InstallationRole.PUBLISHER
        assert InstanceCapabilities().is_publisher


class TestReaderRoutesAreNotOverBlocked:
    """The reader's own features must actually work on a reader.

    An enforcement pass that denied these would be worse than none: it would
    look correct, pass its refusal tests, and leave an installation that cannot
    answer a question.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/search",
            "/api/v1/chat/sessions",
            "/api/v1/feedback",
            "/api/v1/auth/login",
            "/api/v1/exports/{release_id}",
        ],
    )
    def test_a_reader_feature_is_permitted(self, path: str) -> None:
        route = next(
            route for route in _mutating_routes() if route.path == path and route.methods & {"POST"}
        )
        capability = _declared(route)
        assert capability is not None
        assert InstanceCapabilities(role=InstallationRole.READER).has(capability), (
            f"{path} is a reader feature and declares {capability.value}, which a "
            "reader does not have"
        )
