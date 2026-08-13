"""Domain: entities, invariants, and state machines.

No HTTP, no SQLAlchemy, no provider SDK, no filesystem. Enforced by
``tests/contract/test_module_dependencies.py``.
"""

from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    AcquisitionRoute,
    Asset,
    Manifestation,
    RightsDecision,
    asset_parse_blockers,
)
from lab_assistant.domain.chat import (
    ChatMessage,
    ChatSession,
    Citation,
    CitationStatus,
    Feedback,
    derive_session_title,
)
from lab_assistant.domain.claims import (
    Claim,
    ClaimRevision,
    Entity,
    Experiment,
    Review,
    ReviewDecision,
    claim_release_blockers,
)
from lab_assistant.domain.identity import (
    IdentityDecision,
    Work,
    WorkIdentifier,
    WorkKind,
    WorkRelation,
    assess_merge,
    normalise_identifier,
    propose_duplicates,
)
from lab_assistant.domain.normalisation import (
    SPAN_NORMALISATION_VERSION,
    verify_quote,
)
from lab_assistant.domain.observation import (
    DiscoveryRun,
    DiscoveryRunStatus,
    Predicate,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
)
from lab_assistant.domain.release import (
    CorpusRelease,
    ReleaseItem,
    SearchAlias,
    compute_release_diff,
)
from lab_assistant.domain.renditions import (
    EvidenceSpan,
    ParseRun,
    Rendition,
    build_evidence_span,
)
from lab_assistant.domain.scope import ConfigVersion, ScopeVersion
from lab_assistant.domain.states import (
    AcquisitionState,
    ExtractionState,
    IdentityState,
    ParseState,
    ReleaseState,
    RelevanceState,
    ReviewState,
    RightsState,
    TaskState,
)
from lab_assistant.domain.tasks import RateBudget, Task
from lab_assistant.domain.workspace import (
    Capability,
    Group,
    Invitation,
    Membership,
    Role,
    User,
    Workspace,
)

__all__ = [
    "SPAN_NORMALISATION_VERSION",
    "AcquisitionAttempt",
    "AcquisitionPlan",
    "AcquisitionRoute",
    "AcquisitionState",
    "Asset",
    "Capability",
    "ChatMessage",
    "ChatSession",
    "Citation",
    "CitationStatus",
    "Claim",
    "ClaimRevision",
    "ConfigVersion",
    "CorpusRelease",
    "DiscoveryRun",
    "DiscoveryRunStatus",
    "Entity",
    "EvidenceSpan",
    "Experiment",
    "ExtractionState",
    "Feedback",
    "Group",
    "IdentityDecision",
    "IdentityState",
    "Invitation",
    "Manifestation",
    "Membership",
    "ParseRun",
    "ParseState",
    "Predicate",
    "RateBudget",
    "ReleaseItem",
    "ReleaseState",
    "RelevanceState",
    "Rendition",
    "Review",
    "ReviewDecision",
    "ReviewState",
    "RightsDecision",
    "RightsState",
    "Role",
    "ScopeVersion",
    "SearchAlias",
    "SourceAssertion",
    "SourceObservation",
    "SourceRequest",
    "Task",
    "TaskState",
    "User",
    "Work",
    "WorkIdentifier",
    "WorkKind",
    "WorkRelation",
    "Workspace",
    "assess_merge",
    "asset_parse_blockers",
    "build_evidence_span",
    "claim_release_blockers",
    "compute_release_diff",
    "derive_session_title",
    "normalise_identifier",
    "propose_duplicates",
    "verify_quote",
]
