"""Stage 6: structured extraction.

Five steps, in this order, and the order is the point:

1. **deterministic metadata** comes from resolved source assertions. A model is
   never asked to rediscover a DOI or an author list from partial text;
2. **evidence selection** searches the *whole* eligible rendition and records
   what it did not search, so an unsearched region becomes
   ``not_searched_budget`` and never ``not_reported``;
3. **proposal generation** may use deterministic recognisers and an approved
   structured-output model, subject to the model-transfer policy;
4. **mechanical validation** verifies every span against the rendition under the
   normalisation contract, checks numbers against their evidence, and rejects
   evidence drawn from the reference list;
5. **abstention classification** distinguishes the ten states that the legacy
   "Not reported" collapsed into one.

A proposal that fails validation is kept as a failed attempt for diagnosis. It
does not become a claim awaiting scientific approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.parsing import ParsingService
from lab_assistant.contracts.archive import StageOutcome, StageResultEnvelope
from lab_assistant.contracts.errors import (
    AbstentionReason,
    ErrorClass,
    NotFound,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.extraction import (
    ClaimProposal,
    ExtractionContext,
    ExtractionRequest,
    ProposalSource,
    TemplateVersionSpec,
    ValidationIssue,
)
from lab_assistant.contracts.parsing import NodeKind, RenditionDocument
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
)
from lab_assistant.domain.claims import (
    Claim,
    ClaimRevision,
    EpistemicStatus,
    Experiment,
    parse_numeric,
)
from lab_assistant.domain.renditions import build_evidence_span
from lab_assistant.domain.stage_results import evaluate_reuse
from lab_assistant.domain.states import ReviewState
from lab_assistant.policy.model_transfer import TransferRequest
from lab_assistant.provenance.fingerprints import extraction_item_fingerprint, task_fingerprint

__all__ = [
    "PROMPT_BUILDER_VERSION",
    "SELECTOR_VERSION",
    "VALIDATOR_VERSION",
    "EvidenceSelection",
    "ExtractionHandler",
    "select_evidence",
    "validate_proposal",
]

IMPLEMENTATION_VERSION = "extraction/1"
SELECTOR_VERSION = "exhaustive-structural/1"
PROMPT_BUILDER_VERSION = "template-prompt/1"
VALIDATOR_VERSION = "mechanical/1"

#: Characters of evidence one extraction request may carry. Exceeding it does not
#: truncate silently: the remaining nodes are reported as unsearched.
DEFAULT_EVIDENCE_BUDGET_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    selected: tuple[str, ...]
    unsearched: tuple[str, ...]
    text: str

    @property
    def exhaustive(self) -> bool:
        return not self.unsearched


def select_evidence(
    document: RenditionDocument,
    *,
    template: TemplateVersionSpec,
    budget_chars: int = DEFAULT_EVIDENCE_BUDGET_CHARS,
) -> EvidenceSelection:
    """Select evidence windows across the whole rendition.

    Scoring is lexical and structural: nodes whose section label or text matches a
    target's hints or predicate terms rank first, tables rank highly because
    measurements live in them, and reference lists are excluded entirely. Every
    eligible node that does not fit the budget is reported, which is what makes
    the ``not_searched_budget`` abstention honest.
    """
    terms: set[str] = set()
    for target in template.targets:
        terms.update(part.casefold() for part in target.predicate.replace("_", " ").split())
        terms.update(hint.casefold() for hint in target.likely_sections)
        if target.unit_expectation:
            terms.add(target.unit_expectation.casefold())

    candidates: list[tuple[float, int, str, str]] = []
    for order, node in enumerate(document.walk()):
        if not node.is_focal or node.kind in (NodeKind.REFERENCE_LIST, NodeKind.REFERENCE):
            continue
        if node.kind is NodeKind.TABLE:
            text = "\n".join(f"{cell.text}" for cell in node.cells if cell.text.strip())
        else:
            text = node.text
        if not text.strip():
            continue
        haystack = f"{node.label or ''} {node.title or ''} {text}".casefold()
        hits = sum(1 for term in terms if term and term in haystack)
        structural_bonus = {
            NodeKind.TABLE: 2.0,
            NodeKind.ABSTRACT: 1.0,
            NodeKind.TABLE_CAPTION: 0.5,
        }.get(node.kind, 0.0)
        score = hits + structural_bonus
        candidates.append((score, order, node.node_id, text))

    # Highest score first; ties keep document order so selection is deterministic.
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
    selected: list[str] = []
    chunks: list[str] = []
    unsearched: list[str] = []
    used = 0
    for _score, _order, node_id, text in ranked:
        block = f"[{node_id}] {text}"
        if used + len(block) > budget_chars and selected:
            unsearched.append(node_id)
            continue
        selected.append(node_id)
        chunks.append(block)
        used += len(block)

    # Restore document order for the prompt so the model sees the paper's flow.
    order_index = {node_id: order for _s, order, node_id, _t in ranked}
    paired = sorted(zip(selected, chunks, strict=True), key=lambda pair: order_index[pair[0]])
    return EvidenceSelection(
        selected=tuple(node_id for node_id, _ in paired),
        unsearched=tuple(sorted(unsearched, key=lambda n: order_index[n])),
        text="\n\n".join(chunk for _, chunk in paired),
    )


@dataclass(slots=True)
class _ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    span_ids: list[UUID] = field(default_factory=list)


def validate_proposal(
    proposal: ClaimProposal,
    *,
    document: RenditionDocument,
    template: TemplateVersionSpec,
    workspace_id: UUID,
    rendition_id: UUID,
    span_id_factory,
    created_at,
    supplied_node_ids: frozenset[str],
) -> tuple[_ValidationResult, list]:
    """Mechanical validation. Everything checkable is checked here."""
    issues: list[ValidationIssue] = []
    spans: list = []

    target = template.target(proposal.target_id)
    if target is None:
        issues.append(
            ValidationIssue(
                code="unknown_target",
                detail=f"target {proposal.target_id!r} is not in template {template.logical_key}",
                target_id=proposal.target_id,
            )
        )
        return _ValidationResult(valid=False, issues=issues), spans

    reports_a_value = proposal.abstention is AbstentionReason.REPORTED
    if reports_a_value and not proposal.evidence:
        issues.append(
            ValidationIssue(
                code="missing_evidence",
                detail="a reported value must carry at least one evidence pointer",
                target_id=proposal.target_id,
            )
        )

    for reference in proposal.evidence:
        if reference.node_id not in supplied_node_ids:
            issues.append(
                ValidationIssue(
                    code="evidence_outside_supplied_selection",
                    detail=(
                        "evidence cites a node that was not supplied to the extractor; "
                        "the pointer is not verifiable"
                    ),
                    target_id=proposal.target_id,
                    node_id=reference.node_id,
                )
            )
            continue
        span, verification = build_evidence_span(
            span_id=span_id_factory(),
            workspace_id=workspace_id,
            rendition_id=rendition_id,
            rendition=document,
            node_id=reference.node_id,
            quote=reference.quote,
            expected_start=reference.start_offset,
            created_at=created_at,
        )
        if span is None:
            issues.append(
                ValidationIssue(
                    code=verification.failure_code or "span_unverified",
                    detail=verification.detail or "evidence quote does not match the rendition",
                    target_id=proposal.target_id,
                    node_id=reference.node_id,
                )
            )
            continue
        if not span.is_focal:
            issues.append(
                ValidationIssue(
                    code="evidence_not_focal",
                    detail="evidence comes from cited literature, not the focal study",
                    target_id=proposal.target_id,
                    node_id=reference.node_id,
                )
            )
            continue
        spans.append(span)

    if target.object_kind == "quantity" and proposal.abstention is AbstentionReason.REPORTED:
        numeric = parse_numeric(proposal.value_verbatim or "")
        if numeric is None:
            issues.append(
                ValidationIssue(
                    code="unparseable_quantity",
                    detail=f"value {proposal.value_verbatim!r} is not a number or interval",
                    target_id=proposal.target_id,
                )
            )
        else:
            quoted = " ".join(reference.quote for reference in proposal.evidence)
            if numeric.value is not None and not _number_in_text(numeric.value, quoted):
                issues.append(
                    ValidationIssue(
                        code="value_not_in_evidence",
                        detail=(
                            f"value {proposal.value_verbatim!r} does not appear in its "
                            "own evidence quote"
                        ),
                        target_id=proposal.target_id,
                    )
                )
        if target.unit_expectation and not (proposal.unit_verbatim or proposal.unit_canonical):
            issues.append(
                ValidationIssue(
                    code="missing_unit",
                    detail=f"target expects a unit ({target.unit_expectation})",
                    target_id=proposal.target_id,
                )
            )

    if target.controlled_vocabulary and proposal.value_verbatim:
        allowed = {term.casefold() for term in target.controlled_vocabulary}
        if proposal.value_verbatim.casefold() not in allowed:
            issues.append(
                ValidationIssue(
                    code="outside_controlled_vocabulary",
                    detail=(
                        f"value {proposal.value_verbatim!r} is not one of "
                        f"{sorted(target.controlled_vocabulary)}"
                    ),
                    target_id=proposal.target_id,
                )
            )

    if target.deterministic_metadata and proposal.proposal_source is ProposalSource.MODEL:
        issues.append(
            ValidationIssue(
                code="model_proposed_deterministic_metadata",
                detail="this target is populated from resolved source assertions, not by a model",
                target_id=proposal.target_id,
            )
        )

    return _ValidationResult(valid=not issues, issues=issues, span_ids=[s.id for s in spans]), spans


def _number_in_text(value: Decimal, text: str) -> bool:
    """Whether ``value`` appears in ``text`` within parsing tolerance."""
    normalised = text.replace(",", "").replace("−", "-")
    candidates = {
        format(value.normalize(), "f"),
        str(value),
    }
    if value == value.to_integral_value():
        candidates.add(str(int(value)))
    return any(candidate and candidate in normalised for candidate in candidates)


class ExtractionHandler:
    stage_name = StageName.EXTRACTION
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._parsing = ParsingService(context)

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        rendition_id = UUID(str(command.inputs["rendition_id"]))
        template_key = command.inputs.get("template")

        async with context.uow_factory() as uow:
            rendition = await uow.renditions.get_rendition(workspace_id, rendition_id)
            if rendition is None:
                raise NotFound(f"rendition {rendition_id} not found")
            if not rendition.is_extraction_eligible:
                raise ValidationFailure(
                    "rendition is not extraction-eligible",
                    context={"quality_status": rendition.quality_status.value},
                )
            template = await self._load_template(uow, workspace_id, template_key)
            assertions = (
                await uow.observations.list_assertions(workspace_id, work_id=rendition.work_id)
                if rendition.work_id
                else []
            )
            asset = await uow.assets.get_asset(workspace_id, rendition.asset_id)
            rights = (
                await uow.policies.get_rights_decision(workspace_id, asset.rights_decision_id)
                if asset and asset.rights_decision_id
                else None
            )

        if template is None:
            return StageResult(
                stage=StageName.EXTRACTION,
                status=StageStatus.SUCCEEDED_NOOP,
                metrics={"reason": "no extraction template configured for this workspace"},
            )

        document = await self._parsing.load_document(workspace_id, rendition)

        # Step 1: deterministic metadata, from assertions rather than a model.
        from lab_assistant.application.reconciliation import canonical_metadata

        deterministic = canonical_metadata(list(assertions))
        deterministic.pop("_provenance", None)

        # Step 2: evidence selection over the whole eligible rendition.
        selection = select_evidence(document, template=template)

        # Step 3: policy gate, then proposal generation.
        provider_properties = (
            context.llm_provider.properties if context.llm_provider is not None else None
        )
        transfer = context.model_transfer_policy.evaluate(
            TransferRequest(
                workspace_id=workspace_id,
                granted_actions=(rights.allowed_actions if rights else frozenset()),
                classification=("public" if rights and rights.licence_expression else "restricted"),
                provider=provider_properties,
                intended_action=PolicyAction.SEND_TO_EXTERNAL_MODEL,
                payload_characters=len(selection.text),
                now=context.now(),
            )
        )

        extraction_context = ExtractionContext(
            workspace_id=workspace_id,
            work_id=rendition.work_id or rendition.id,
            rendition_id=rendition.id,
            rendition_digest=rendition.sha256,
            template=template,
            selected_nodes=selection.selected,
            unsearched_nodes=selection.unsearched,
            evidence_text=selection.text,
            deterministic_metadata=deterministic,
            prompt_bundle_id="extraction-default",
            prompt_bundle_version="1",
            allow_external_model=transfer.external_allowed,
        )

        extractor = context.extractor
        if extractor is None:
            return StageResult(
                stage=StageName.EXTRACTION,
                status=StageStatus.BLOCKED,
                error_class=ErrorClass.POLICY_BLOCKED,
                error_detail="no extractor is configured",
            )

        item_fingerprint = extraction_item_fingerprint(
            extraction_context,
            selector_version=SELECTOR_VERSION,
            prompt_builder_version=PROMPT_BUILDER_VERSION,
            validator_version=VALIDATOR_VERSION,
            parser_version=rendition.schema_version,
            provider=(provider_properties.provider_id if provider_properties else "none"),
            model=context.settings.llm_model,
            model_revision=context.settings.llm_model,
            model_settings={"temperature": context.settings.llm_temperature},
            output_mode="structured_json",
            policy_version_id=command.policy_version_id,
            span_digests=tuple(
                (document.text_of(node_id) or "")[:64] for node_id in selection.selected
            ),
        )

        # Reuse is decided by what the last run *reported*, not by whether it
        # left rows behind. Asking for a claim revision meant a paper the
        # extractor read and found nothing in looked unprocessed — every target
        # abstaining is a result, not an absence — so it went back to the model
        # on every restart, forever, for every silent paper (audit F10).
        async with context.uow_factory() as uow:
            recorded = await uow.stage_results.find(workspace_id, item_fingerprint)
        decision = evaluate_reuse(recorded.outcome if recorded else None)
        if decision.reuse:
            return StageResult(
                stage=StageName.EXTRACTION,
                status=(
                    StageStatus.FAILED if decision.carries_failure else StageStatus.SUCCEEDED_NOOP
                ),
                error_class=(
                    ErrorClass(recorded.error_class)
                    if decision.carries_failure and recorded and recorded.error_class
                    else None
                ),
                error_detail=decision.detail if decision.carries_failure else None,
                metrics={
                    "reused": True,
                    "recorded_outcome": decision.outcome.value if decision.outcome else None,
                    "reuse_detail": decision.detail,
                    "fingerprint": item_fingerprint,
                },
            )

        result = await extractor.propose(ExtractionRequest(context=extraction_context))

        # Step 4 and 5: validation and abstention classification.
        supplied = frozenset(selection.selected)
        accepted = 0
        rejected = 0
        experiments: dict[str, UUID] = {}
        issues_by_target: dict[str, list[str]] = {}

        async with context.uow_factory() as uow:
            for proposal in result.proposals:
                proposal = _reclassify_abstention(proposal, selection)
                validation, spans = validate_proposal(
                    proposal,
                    document=document,
                    template=template,
                    workspace_id=workspace_id,
                    rendition_id=rendition.id,
                    span_id_factory=context.id_factory,
                    created_at=context.now(),
                    supplied_node_ids=supplied,
                )
                if spans:
                    await uow.renditions.add_spans(spans)

                experiment_id: UUID | None = None
                label = proposal.experiment_label or "default"
                if rendition.work_id is not None:
                    if label not in experiments:
                        experiment = Experiment(
                            id=context.new_id(),
                            workspace_id=workspace_id,
                            work_id=rendition.work_id,
                            rendition_id=rendition.id,
                            local_label=label,
                            created_at=context.now(),
                        )
                        await uow.claims.add_experiment(experiment)
                        experiments[label] = experiment.id
                    experiment_id = experiments[label]

                claim = Claim(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    work_id=rendition.work_id or rendition.id,
                    experiment_id=experiment_id,
                    subject_label=proposal.subject_label,
                    predicate=proposal.predicate,
                    object_kind=proposal.object_kind,
                    object_value={
                        "verbatim": proposal.value_verbatim,
                        "normalised": proposal.value_normalised,
                        "unit_verbatim": proposal.unit_verbatim,
                        "unit_canonical": proposal.unit_canonical,
                        "basis": proposal.basis,
                    },
                    qualifiers=proposal.qualifiers,
                    polarity=proposal.polarity,
                    epistemic_status=(
                        EpistemicStatus.ASSERTED_BY_SOURCE
                        if proposal.abstention is AbstentionReason.REPORTED
                        else EpistemicStatus.ABSENT_FROM_SOURCE
                    ),
                    template_id=template.template_id,
                    template_version=template.semantic_version,
                    target_id=proposal.target_id,
                    lifecycle_status="proposed",
                    created_at=context.now(),
                )
                revision = ClaimRevision(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    claim_id=claim.id,
                    revision_number=1,
                    value=dict(claim.object_value),
                    evidence_span_ids=tuple(validation.span_ids),
                    proposal_source=proposal.proposal_source,
                    extractor_fingerprint=item_fingerprint,
                    validation_status="valid" if validation.valid else "invalid",
                    validation_issues=tuple(
                        f"{issue.code}: {issue.detail}" for issue in validation.issues
                    ),
                    review_status=ReviewState.UNREVIEWED,
                    confidence_components=proposal.confidence_components,
                    abstention_reason=proposal.abstention,
                    epistemic_status=claim.epistemic_status,
                    created_at=context.now(),
                )
                claim.current_revision_id = revision.id
                await uow.claims.add_claim(claim)
                await uow.claims.add_revision(revision)
                if validation.valid:
                    accepted += 1
                else:
                    rejected += 1
                    issues_by_target[proposal.target_id] = [
                        issue.code for issue in validation.issues
                    ]

            await uow.audit.append(
                context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.WORKER,
                    actor_id=command.actor_id,
                    action=AuditAction.EXTRACTION_COMPLETED,
                    subject_type="rendition",
                    subject_id=rendition.id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    metadata={
                        "template": template.logical_key,
                        "proposals": len(result.proposals),
                        "valid": accepted,
                        "invalid": rejected,
                        "exhaustive_search": selection.exhaustive,
                        "external_model_allowed": transfer.external_allowed,
                        "transfer_reason": transfer.reason_code,
                        "fingerprint": item_fingerprint,
                    },
                )
            )
            await uow.commit()

        # Recorded whichever way it went. An empty result is the whole reason
        # this index exists: without a row saying "processed, found nothing",
        # the next run cannot tell it from "never tried".
        # "Found nothing" is not "returned nothing". A complete search over a
        # paper that reports none of the targets comes back as a full set of
        # abstentions, which is a positive statement about the source — and the
        # single most expensive thing to misread, because re-running it costs a
        # model call per paper per restart.
        reported_anything = any(
            proposal.abstention is AbstentionReason.REPORTED for proposal in result.proposals
        )
        outcome = (
            StageOutcome.REFUSED_BY_POLICY
            if result.refused
            else StageOutcome.SUCCEEDED_TRUNCATED
            if result.truncated
            else StageOutcome.SUCCEEDED
            if reported_anything
            else StageOutcome.SUCCEEDED_EMPTY
        )
        async with context.uow_factory() as uow:
            await uow.stage_results.record(
                StageResultEnvelope(
                    stage=StageName.EXTRACTION.value,
                    outcome=outcome,
                    reuse_fingerprint=item_fingerprint,
                    output_artifacts=(),
                    tool_versions={
                        "provider": (
                            provider_properties.provider_id if provider_properties else "none"
                        ),
                        "model": context.settings.llm_model,
                        "selector": SELECTOR_VERSION,
                        "prompt_builder": PROMPT_BUILDER_VERSION,
                        "validator": VALIDATOR_VERSION,
                    },
                    policy_scope={
                        "workspace_id": str(workspace_id),
                        "policy_version_id": (
                            str(command.policy_version_id) if command.policy_version_id else None
                        ),
                    },
                    completed_at=context.now(),
                    actor_id=command.actor_id,
                ),
                workspace_id=workspace_id,
            )
            await uow.commit()

        return StageResult(
            stage=StageName.EXTRACTION,
            status=StageStatus.SUCCEEDED if result.proposals else StageStatus.SUCCEEDED_NOOP,
            metrics={
                "outcome": outcome.value,
                "proposals": len(result.proposals),
                "valid": accepted,
                "invalid": rejected,
                "experiments": len(experiments),
                "selected_nodes": len(selection.selected),
                "unsearched_nodes": len(selection.unsearched),
                "truncated": result.truncated,
                "refused": result.refused,
                "invalid_targets": issues_by_target,
            },
            quality_gates={
                "evidence_verified_for_valid_proposals": True,
                "exhaustive_search": selection.exhaustive,
                "no_model_authored_metadata": all(
                    "model_proposed_deterministic_metadata" not in codes
                    for codes in issues_by_target.values()
                ),
            },
            # Review is a human queue, not a machine stage: validated proposals
            # wait in the review queue rather than scheduling a task nobody can
            # execute. A human queue is an explicit branch, never a pending task.
            human_action_required=(
                f"{accepted} claim proposals await review" if accepted else None
            ),
        )

    async def _load_template(
        self, uow, workspace_id: UUID, template_key: Any
    ) -> TemplateVersionSpec | None:
        """Load the workspace's extraction template.

        Templates are workspace data with stable ids and versions. There is no
        domain template compiled into core code.
        """
        from lab_assistant.domain.scope import ConfigKind

        configs = await uow.configs.list_config_versions(
            workspace_id, kind=ConfigKind.EXTRACTION_TEMPLATE.value
        )
        if not configs:
            return None
        if template_key:
            key = str(template_key)
            exact = [
                config
                for config in configs
                if f"{config.logical_id}@{config.semantic_version}" == key
            ]
            if exact:
                return TemplateVersionSpec.model_validate(exact[0].payload)
            # A bare template id means its approved version, else its newest.
            named = [config for config in configs if config.logical_id == key]
            if named:
                from lab_assistant.application.datasheets import active_template

                chosen = active_template(named) or max(
                    named, key=lambda config: config.semantic_version
                )
                return TemplateVersionSpec.model_validate(chosen.payload)
            raise NotFound(f"extraction template {template_key} not found")
        # No template named: the approved one. A draft never fills the review queue.
        from lab_assistant.application.datasheets import active_template

        active = active_template(configs)
        return TemplateVersionSpec.model_validate(active.payload) if active else None


def _reclassify_abstention(proposal: ClaimProposal, selection: EvidenceSelection) -> ClaimProposal:
    """Downgrade an over-confident absence claim.

    If the extractor says "not reported after a complete search" but the selector
    could not cover the rendition, the honest state is ``not_searched_budget``.
    """
    if (
        proposal.abstention is AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH
        and not selection.exhaustive
    ):
        return proposal.model_copy(update={"abstention": AbstentionReason.NOT_SEARCHED_BUDGET})
    return proposal
