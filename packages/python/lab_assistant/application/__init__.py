"""Application layer: lifecycle use cases and transaction boundaries.

Services never touch a database session or an HTTP request. They take a
``ServiceContext`` and an ``ActorContext``, open a unit of work, and commit.
"""

from lab_assistant.application.accounts import AccountService, PasswordHasher
from lab_assistant.application.acquisition import (
    AcquisitionHandler,
    AcquisitionPlanningHandler,
    AcquisitionService,
)
from lab_assistant.application.analytics import AnalyticsService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.context import ActorContext, ServiceContext, resolve_actor
from lab_assistant.application.discovery import DiscoveryHandler, DiscoveryService
from lab_assistant.application.export import ExportService, validate_package
from lab_assistant.application.extraction import ExtractionHandler
from lab_assistant.application.fetching import AcquisitionSweepHandler, FetchQueue
from lab_assistant.application.indexing import IndexingHandler
from lab_assistant.application.parsing import ParsingHandler, ParsingService
from lab_assistant.application.reconciliation import IdentityService, ReconciliationHandler
from lab_assistant.application.refresh import RefreshService
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.application.review import ReviewService
from lab_assistant.application.scope_deletion import ScopeDeletionHandler
from lab_assistant.application.scopes import ScopeService
from lab_assistant.application.tasks import HandlerRegistry, TaskOrchestrator

__all__ = [
    "AccountService",
    "AcquisitionHandler",
    "AcquisitionPlanningHandler",
    "AcquisitionService",
    "AcquisitionSweepHandler",
    "ActorContext",
    "AnalyticsService",
    "ChatService",
    "DiscoveryHandler",
    "DiscoveryService",
    "ExportService",
    "ExtractionHandler",
    "FetchQueue",
    "HandlerRegistry",
    "IdentityService",
    "IndexingHandler",
    "ParsingHandler",
    "ParsingService",
    "PasswordHasher",
    "ReconciliationHandler",
    "RefreshService",
    "ReleaseDraftRequest",
    "ReleaseService",
    "RetrievalService",
    "ReviewService",
    "ScopeDeletionHandler",
    "ScopeService",
    "ServiceContext",
    "TaskOrchestrator",
    "build_handler_registry",
    "resolve_actor",
    "validate_package",
]


def build_handler_registry(context: ServiceContext) -> HandlerRegistry:
    """Wire every stage handler for a worker or an inline test run."""
    registry = HandlerRegistry()
    registry.register(DiscoveryHandler(context))
    registry.register(ReconciliationHandler(context))
    registry.register(AcquisitionPlanningHandler(context))
    registry.register(AcquisitionSweepHandler(context))
    registry.register(AcquisitionHandler(context))
    registry.register(ParsingHandler(context))
    registry.register(ExtractionHandler(context))
    registry.register(IndexingHandler(context))
    registry.register(ScopeDeletionHandler(context))
    return registry
