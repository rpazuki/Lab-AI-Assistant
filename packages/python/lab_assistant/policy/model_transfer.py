"""External model transfer policy.

Evaluated twice: when the task is created and again immediately before the
provider call. Restricted content falls back to a local model or is not
processed. The UI explains the outcome without exposing policy internals.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.models import ProviderProperties
from lab_assistant.contracts.policy import PolicyAction

__all__ = ["ModelTransferPolicy", "TransferRequest", "TransferVerdict"]


class TransferOutcome(StrEnum):
    ALLOW_EXTERNAL = "allow_external"
    ALLOW_LOCAL_ONLY = "allow_local_only"
    DENY = "deny"


class TransferRequest(BaseModel):
    model_config = {"frozen": True}

    workspace_id: UUID
    #: Rights actions already granted for this material.
    granted_actions: frozenset[PolicyAction] = frozenset()
    classification: str = "public"  # public | internal | restricted | confidential
    contains_personal_data: bool = False
    provider: ProviderProperties | None = None
    intended_action: PolicyAction = PolicyAction.SEND_TO_EXTERNAL_MODEL
    #: Number of evidence characters about to be sent, for minimum-necessary checks.
    payload_characters: int = 0
    max_payload_characters: int = 200_000
    now: datetime | None = None


class TransferVerdict(BaseModel):
    model_config = {"frozen": True}

    outcome: TransferOutcome
    reason_code: str
    #: Safe, user-facing explanation. Never contains policy internals.
    user_message: str
    conditions: tuple[str, ...] = ()
    evidence: dict[str, object] = Field(default_factory=dict)

    @property
    def external_allowed(self) -> bool:
        return self.outcome is TransferOutcome.ALLOW_EXTERNAL


class ModelTransferPolicy(BaseModel):
    model_config = {"frozen": True}

    policy_id: str = "model-transfer-default"
    semantic_version: str = "1.0.0"
    #: Classifications that may leave the controlled environment at all.
    externalisable_classifications: tuple[str, ...] = ("public",)
    #: Providers approved for this deployment, by provider id.
    approved_providers: tuple[str, ...] = ()
    forbid_personal_data_externally: bool = True
    require_no_training_use: bool = True
    require_no_retention: bool = False

    def evaluate(self, request: TransferRequest) -> TransferVerdict:
        if request.intended_action not in (
            PolicyAction.SEND_TO_EXTERNAL_MODEL,
            PolicyAction.CREATE_EMBEDDINGS_EXTERNAL,
        ):
            return TransferVerdict(
                outcome=TransferOutcome.ALLOW_LOCAL_ONLY,
                reason_code="local_action",
                user_message="Processed locally.",
            )

        if request.intended_action not in request.granted_actions:
            return self._local_or_deny(
                "rights_do_not_permit_external_transfer",
                "This material may only be processed locally, so an external model was not used.",
                request,
            )

        if request.classification not in self.externalisable_classifications:
            return self._local_or_deny(
                "classification_not_externalisable",
                (
                    "This collection is classified for local processing only. "
                    "A local model was used where available."
                ),
                request,
            )

        if request.contains_personal_data and self.forbid_personal_data_externally:
            return self._local_or_deny(
                "personal_data_present",
                "This material contains personal data and was not sent to an external service.",
                request,
            )

        provider = request.provider
        if provider is None or provider.provider_id not in self.approved_providers:
            return self._local_or_deny(
                "provider_not_approved",
                "No approved external model provider is configured for this workspace.",
                request,
            )
        if self.require_no_training_use and provider.trains_on_data:
            return self._local_or_deny(
                "provider_trains_on_data",
                "The configured provider's terms do not meet this deployment's policy.",
                request,
            )
        if self.require_no_retention and provider.retains_data:
            return self._local_or_deny(
                "provider_retains_data",
                "The configured provider's retention terms do not meet this deployment's policy.",
                request,
            )
        if request.payload_characters > request.max_payload_characters:
            return self._local_or_deny(
                "payload_exceeds_minimum_necessary",
                "The evidence selection was larger than this workspace permits to send.",
                request,
            )

        return TransferVerdict(
            outcome=TransferOutcome.ALLOW_EXTERNAL,
            reason_code="approved_external_transfer",
            user_message="Answer generated with the approved model provider.",
            conditions=("minimum necessary evidence", "no tool access during synthesis"),
            evidence={
                "provider": provider.provider_id,
                "classification": request.classification,
                "payload_characters": request.payload_characters,
            },
        )

    @staticmethod
    def _local_or_deny(reason: str, message: str, request: TransferRequest) -> TransferVerdict:
        local_permitted = PolicyAction.PROCESS_LOCAL in request.granted_actions
        return TransferVerdict(
            outcome=(TransferOutcome.ALLOW_LOCAL_ONLY if local_permitted else TransferOutcome.DENY),
            reason_code=reason,
            user_message=message,
            evidence={"classification": request.classification},
        )
