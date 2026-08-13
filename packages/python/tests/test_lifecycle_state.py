"""State machines, task semantics, and the release lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from lab_assistant.contracts.errors import (
    AbstentionReason,
    AcquisitionOutcome,
    ErrorClass,
    SourceOutcome,
    StateTransitionError,
    is_retryable,
    retry_eligibility,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.release import ReleaseItemRecord, ReleaseManifest
from lab_assistant.contracts.runtime import FixedClock, SequentialIds
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    Asset,
    MalwareScanStatus,
    RightsDecision,
    ValidationStatus,
    asset_parse_blockers,
)
from lab_assistant.domain.observation import ConnectorRunState, DiscoveryRun, DiscoveryRunStatus
from lab_assistant.domain.release import (
    CorpusRelease,
    SearchAlias,
    compute_release_diff,
    release_transition_allowed,
)
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
    WorkflowLabel,
    derive_workflow_label,
)
from lab_assistant.domain.tasks import Task, backoff_seconds, next_state_for_error
from lab_assistant.provenance.audit import AuditTrail, verify_chain

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
USER = UUID("00000000-0000-4000-8000-000000000002")


class TestOutcomeTaxonomies:
    def test_only_clean_source_outcomes_are_acceptable_terminals(self) -> None:
        acceptable = {o for o in SourceOutcome if o.is_acceptable_terminal}
        assert acceptable == {SourceOutcome.SUCCEEDED, SourceOutcome.SUCCEEDED_EMPTY}

    def test_rate_limited_source_outcome_is_not_terminal(self) -> None:
        assert not SourceOutcome.RATE_LIMITED_RETRYABLE.is_terminal
        assert SourceOutcome.POLICY_BLOCKED.is_terminal

    def test_acquisition_states_are_not_collapsed(self) -> None:
        assert AcquisitionOutcome.ASSISTED_REQUIRED.needs_human
        assert AcquisitionOutcome.ENTITLEMENT_REQUIRED.needs_human
        assert AcquisitionOutcome.ALREADY_AVAILABLE.produced_asset
        assert not AcquisitionOutcome.METADATA_ONLY.produced_asset

    def test_only_two_abstentions_support_a_statement_about_the_source(self) -> None:
        supporting = {r for r in AbstentionReason if r.supports_source_statement}
        assert supporting == {
            AbstentionReason.REPORTED,
            AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH,
        }

    @pytest.mark.parametrize("error_class", sorted(ErrorClass, key=lambda e: e.value))
    def test_every_error_class_has_a_retry_policy(self, error_class: ErrorClass) -> None:
        assert retry_eligibility(error_class) is not None

    def test_policy_and_malware_failures_are_never_retried(self) -> None:
        assert not is_retryable(ErrorClass.POLICY_BLOCKED)
        assert not is_retryable(ErrorClass.MALWARE_DETECTED)
        assert is_retryable(ErrorClass.RATE_LIMITED)


class TestWorkflowLabel:
    def _label(self, **overrides: object) -> WorkflowLabel:
        base: dict[str, object] = {
            "identity": IdentityState.RESOLVED,
            "relevance": RelevanceState.INCLUDED,
            "rights": RightsState.PERMITTED,
            "acquisition": AcquisitionState.ACQUIRED,
            "parse": ParseState.READY,
            "extraction": ExtractionState.VALIDATED,
            "review": ReviewState.ACCEPTED,
            "release": ReleaseState.PUBLISHED,
        }
        base.update(overrides)
        return derive_workflow_label(**base)  # type: ignore[arg-type]

    def test_conflict_dominates(self) -> None:
        assert self._label(identity=IdentityState.CONFLICT) is (
            WorkflowLabel.NEEDS_IDENTITY_DECISION
        )

    def test_assisted_acquisition_surfaces(self) -> None:
        assert self._label(acquisition=AcquisitionState.ASSISTED) is (
            WorkflowLabel.NEEDS_ASSISTED_UPLOAD
        )

    def test_degraded_parse_needs_review(self) -> None:
        assert self._label(parse=ParseState.DEGRADED) is WorkflowLabel.NEEDS_PARSE_REVIEW

    def test_unreviewed_claim_blocks_release_ready(self) -> None:
        assert self._label(review=ReviewState.UNREVIEWED) is WorkflowLabel.NEEDS_CLAIM_REVIEW

    def test_fully_processed_is_published(self) -> None:
        assert self._label() is WorkflowLabel.PUBLISHED


class TestDiscoveryFreezeGate:
    def _run(self, *states: ConnectorRunState) -> DiscoveryRun:
        return DiscoveryRun(
            id=uuid4(),
            workspace_id=WORKSPACE,
            scope_version_id=uuid4(),
            run_fingerprint="discovery_run:abc",
            requested_by=USER,
            connector_states=tuple(states),
        )

    def _state(self, **overrides: object) -> ConnectorRunState:
        base: dict[str, object] = {
            "connector_id": "pubmed",
            "connector_version": "1.0.0",
            "required": True,
            "query_fingerprint": "q",
            "outcome": SourceOutcome.SUCCEEDED,
            "records_seen": 10,
            "declared_total": 10,
        }
        base.update(overrides)
        return ConnectorRunState(**base)  # type: ignore[arg-type]

    def test_clean_run_freezes(self) -> None:
        assert self._run(self._state()).can_freeze

    def test_no_connector_cannot_freeze(self) -> None:
        assert not self._run().can_freeze

    def test_degraded_required_source_blocks_freeze(self) -> None:
        run = self._run(self._state(outcome=SourceOutcome.DEGRADED_PARTIAL))
        assert not run.can_freeze
        assert "not accepted by a steward" in run.freeze_blockers()[0]

    def test_steward_acceptance_unblocks_a_degraded_source(self) -> None:
        run = self._run(
            self._state(outcome=SourceOutcome.DEGRADED_PARTIAL, accepted_degraded_by=USER)
        )
        assert run.can_freeze

    def test_optional_source_failure_does_not_block(self) -> None:
        run = self._run(
            self._state(),
            self._state(
                connector_id="openalex",
                required=False,
                outcome=SourceOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
                records_seen=0,
                declared_total=None,
            ),
        )
        assert run.can_freeze

    def test_pagination_shortfall_blocks_freeze(self) -> None:
        run = self._run(self._state(records_seen=7, declared_total=10))
        assert not run.can_freeze
        assert "pagination integrity" in " ".join(run.freeze_blockers())

    def test_only_a_frozen_run_can_feed_a_release(self) -> None:
        assert DiscoveryRunStatus.FROZEN.can_feed_release
        assert not DiscoveryRunStatus.DEGRADED.can_feed_release
        assert not DiscoveryRunStatus.RUNNING.can_feed_release


class TestAssetParseGate:
    def _asset(self, **overrides: object) -> Asset:
        base: dict[str, object] = {
            "id": uuid4(),
            "workspace_id": WORKSPACE,
            "sha256": "a" * 64,
            "byte_length": 10,
            "object_id": uuid4(),
            "media_type_detected": "application/pdf",
            "malware_scan_status": MalwareScanStatus.PASSED,
            "validation_status": ValidationStatus.PASSED,
            "identity_verified": True,
        }
        base.update(overrides)
        asset = Asset(**base)  # type: ignore[arg-type]
        return asset

    def _rights(self, asset: Asset, **overrides: object) -> RightsDecision:
        base: dict[str, object] = {
            "id": uuid4(),
            "workspace_id": WORKSPACE,
            "subject_type": "asset",
            "subject_id": asset.id,
            "allowed_actions": frozenset({PolicyAction.PROCESS_LOCAL}),
            "status": RightsState.PERMITTED,
        }
        base.update(overrides)
        return RightsDecision(**base)  # type: ignore[arg-type]

    def _blockers(
        self, asset: Asset, rights: RightsDecision | None, **kwargs: object
    ) -> tuple[str, ...]:
        return asset_parse_blockers(
            asset,
            rights=rights,
            permitted_media_types=frozenset({"application/pdf", "application/xml"}),
            now=NOW,
            require_malware_scan=bool(kwargs.get("require_malware_scan", True)),
        )

    def test_clean_asset_is_parse_ready(self) -> None:
        asset = self._asset()
        assert self._blockers(asset, self._rights(asset)) == ()

    def test_no_rights_decision_blocks_processing(self) -> None:
        asset = self._asset()
        assert "no rights decision recorded" in self._blockers(asset, None)

    def test_rights_without_process_local_blocks_processing(self) -> None:
        asset = self._asset()
        rights = self._rights(asset, allowed_actions=frozenset({PolicyAction.VIEW_METADATA}))
        assert any("local processing" in blocker for blocker in self._blockers(asset, rights))

    def test_expired_rights_decision_blocks_processing(self) -> None:
        asset = self._asset()
        rights = self._rights(asset, review_due_at=NOW - timedelta(days=1))
        assert any("review date" in blocker for blocker in self._blockers(asset, rights))

    def test_unscanned_upload_blocks_when_scanning_is_required(self) -> None:
        asset = self._asset(malware_scan_status=MalwareScanStatus.NOT_SCANNED)
        assert "malware scan has not run" in self._blockers(asset, self._rights(asset))

    def test_recorded_skip_is_allowed_when_scanning_is_staged_off(self) -> None:
        asset = self._asset(malware_scan_status=MalwareScanStatus.SKIPPED_WITH_EXCEPTION)
        assert self._blockers(asset, self._rights(asset), require_malware_scan=False) == ()

    def test_failed_scan_blocks_even_when_scanning_is_staged_off(self) -> None:
        asset = self._asset(malware_scan_status=MalwareScanStatus.FAILED)
        blockers = self._blockers(asset, self._rights(asset), require_malware_scan=False)
        assert "malware scan failed" in blockers

    def test_unverified_identity_blocks_processing(self) -> None:
        asset = self._asset(identity_verified=False)
        assert any("identity" in blocker for blocker in self._blockers(asset, self._rights(asset)))

    def test_unpermitted_media_type_blocks_processing(self) -> None:
        asset = self._asset(media_type_detected="application/zip")
        blockers = self._blockers(asset, self._rights(asset))
        assert any("media type" in blocker for blocker in blockers)


class TestTaskSemantics:
    def _task(self, **overrides: object) -> Task:
        base: dict[str, object] = {
            "id": uuid4(),
            "workspace_id": WORKSPACE,
            "stage": StageName.DISCOVERY,
            "state": TaskState.READY,
            "input_fingerprint": "task:abc",
            "created_at": NOW,
        }
        base.update(overrides)
        return Task(**base)  # type: ignore[arg-type]

    def test_backoff_is_deterministic_and_capped(self) -> None:
        assert backoff_seconds(1) == 2
        assert backoff_seconds(3) == 8
        assert backoff_seconds(50) == 900
        assert backoff_seconds(3) == backoff_seconds(3)

    def test_expired_lease_makes_a_task_claimable_again(self) -> None:
        task = self._task()
        task.claim(owner="worker-a", now=NOW, lease_seconds=60)
        assert task.state is TaskState.LEASED
        assert not task.is_claimable(NOW + timedelta(seconds=30))
        assert task.is_claimable(NOW + timedelta(seconds=61))

    def test_human_action_is_not_a_failure(self) -> None:
        task = self._task()
        task.claim(owner="w", now=NOW)
        state = task.fail(
            now=NOW,
            error_class=ErrorClass.ENTITLEMENT_REQUIRED,
            detail="needs a licensed copy",
        )
        assert state is TaskState.AWAITING_USER
        assert not state.counts_as_failure
        assert task.lease_owner is None
        assert task.human_action_required == "entitlement_required"

    def test_retryable_failure_schedules_a_later_attempt(self) -> None:
        task = self._task()
        task.claim(owner="w", now=NOW)
        state = task.fail(
            now=NOW, error_class=ErrorClass.RATE_LIMITED, detail="429", retry_after_seconds=30
        )
        assert state is TaskState.READY
        assert task.not_before == NOW + timedelta(seconds=30)

    def test_permanent_failure_does_not_retry(self) -> None:
        task = self._task()
        task.claim(owner="w", now=NOW)
        assert (
            task.fail(now=NOW, error_class=ErrorClass.INVALID_QUERY, detail="bad query")
            is TaskState.FAILED
        )

    def test_exhausted_retries_dead_letter(self) -> None:
        assert (
            next_state_for_error(ErrorClass.SOURCE_UNAVAILABLE, attempt=5, max_attempts=5)
            is TaskState.DEAD_LETTER
        )

    def test_malware_quarantines_rather_than_failing(self) -> None:
        assert (
            next_state_for_error(ErrorClass.MALWARE_DETECTED, attempt=1, max_attempts=5)
            is TaskState.QUARANTINED
        )

    def test_worker_class_routes_by_stage(self) -> None:
        assert self._task(stage=StageName.DISCOVERY).worker_class == "network"
        assert self._task(stage=StageName.INDEXING).worker_class == "compute"


class TestReleaseLifecycle:
    def _release(self, **overrides: object) -> CorpusRelease:
        base: dict[str, object] = {
            "id": uuid4(),
            "workspace_id": WORKSPACE,
            "name": "rla-corpus",
            "semantic_version": "1.0.0",
            "created_at": NOW,
        }
        base.update(overrides)
        return CorpusRelease(**base)  # type: ignore[arg-type]

    def test_only_declared_transitions_are_legal(self) -> None:
        assert release_transition_allowed(ReleaseState.DRAFT, ReleaseState.VALIDATING)
        assert not release_transition_allowed(ReleaseState.DRAFT, ReleaseState.PUBLISHED)
        assert not release_transition_allowed(ReleaseState.PUBLISHED, ReleaseState.DRAFT)

    def test_published_release_is_immutable(self) -> None:
        release = self._release(status=ReleaseState.PUBLISHED)
        with pytest.raises(StateTransitionError):
            release.transition_to(ReleaseState.DRAFT)

    def test_publish_blockers_name_every_missing_precondition(self) -> None:
        release = self._release(status=ReleaseState.PUBLISHING)
        blockers = release.publish_blockers()
        assert any("manifest" in b for b in blockers)
        assert any("approver" in b for b in blockers)
        assert any("gate results" in b for b in blockers)

    def test_failed_gate_blocks_publication(self) -> None:
        release = self._release(
            status=ReleaseState.PUBLISHING,
            manifest_sha256="a" * 64,
            approved_by=USER,
            gate_results={"evidence_span_validity": False},
        )
        assert any("failed gates" in b for b in release.publish_blockers())

    def test_missing_projection_blocks_publication(self) -> None:
        release = self._release(
            status=ReleaseState.PUBLISHING,
            manifest_sha256="a" * 64,
            approved_by=USER,
            gate_results={"ok": True},
            embedding_profile_ids=("hashing-v1",),
        )
        assert any("projections not ready" in b for b in release.publish_blockers())

    def test_alias_switch_and_rollback_do_not_rewrite_releases(self) -> None:
        first, second = uuid4(), uuid4()
        alias = SearchAlias(workspace_id=WORKSPACE, alias="current")
        alias.switch_to(first, by=USER, at=NOW)
        alias.switch_to(second, by=USER, at=NOW)
        assert alias.release_id == second
        assert alias.rollback(by=USER, at=NOW) == first
        assert alias.release_id == first
        assert alias.previous_release_id == second

    def test_rollback_without_history_is_refused(self) -> None:
        alias = SearchAlias(workspace_id=WORKSPACE, alias="current")
        with pytest.raises(StateTransitionError):
            alias.rollback(by=USER, at=NOW)


class TestReleaseManifestAndDiff:
    def _manifest(self, release_id: UUID, records: list[ReleaseItemRecord]) -> ReleaseManifest:
        return ReleaseManifest(
            release_id=release_id,
            workspace_id=WORKSPACE,
            name="corpus",
            semantic_version="1.0.0",
            items=tuple(records),
        )

    def test_manifest_hash_is_order_independent(self) -> None:
        first, second = uuid4(), uuid4()
        left = ReleaseItemRecord(work_id=first)
        right = ReleaseItemRecord(work_id=second)
        release = uuid4()
        assert (
            self._manifest(release, [left, right]).manifest_sha256
            == self._manifest(release, [right, left]).manifest_sha256
        )

    def test_changed_rendition_changes_only_that_item_digest(self) -> None:
        work = uuid4()
        rendition = uuid4()
        before = ReleaseItemRecord(work_id=work, rendition_id=rendition, rendition_digest="a")
        after = ReleaseItemRecord(work_id=work, rendition_id=rendition, rendition_digest="b")
        assert before.content_digest() != after.content_digest()

    def test_diff_separates_added_changed_and_unchanged(self) -> None:
        stable, changing, added, removed = uuid4(), uuid4(), uuid4(), uuid4()
        parent = self._manifest(
            uuid4(),
            [
                ReleaseItemRecord(work_id=stable, rendition_digest="s"),
                ReleaseItemRecord(work_id=changing, rendition_digest="old"),
                ReleaseItemRecord(work_id=removed, rendition_digest="r"),
            ],
        )
        child = self._manifest(
            uuid4(),
            [
                ReleaseItemRecord(work_id=stable, rendition_digest="s"),
                ReleaseItemRecord(work_id=changing, rendition_digest="new"),
                ReleaseItemRecord(work_id=added, rendition_digest="a"),
            ],
        )
        diff = compute_release_diff(manifest=child, parent_manifest=parent)
        assert diff.added_work_ids == (added,)
        assert diff.changed_work_ids == (changing,)
        assert diff.unchanged_work_ids == (stable,)
        assert diff.removed_work_ids == (removed,)
        assert diff.changed_item_count == 3

    def test_first_release_is_a_full_build(self) -> None:
        diff = compute_release_diff(
            manifest=self._manifest(uuid4(), [ReleaseItemRecord(work_id=uuid4())]),
            parent_manifest=None,
        )
        assert diff.is_full_build
        assert len(diff.added_work_ids) == 1


class TestAuditChain:
    def test_chain_detects_a_removed_event(self) -> None:
        trail = AuditTrail(id_factory=SequentialIds(), clock=FixedClock(NOW))
        events = []
        previous = None
        for index in range(3):
            event = trail.build(
                workspace_id=WORKSPACE,
                actor_type=ActorType.USER,
                actor_id=USER,
                action=AuditAction.ANSWER_GENERATED,
                subject_type="chat_message",
                subject_id=uuid4(),
                previous_hash=previous,
                metadata={"index": index},
            )
            events.append(event)
            previous = event.integrity_hash
        assert verify_chain(events) == (True, ())
        broken = [events[0], events[2]]
        ok, problems = verify_chain(broken)
        assert not ok and problems

    def test_secrets_and_content_are_redacted_from_metadata(self) -> None:
        trail = AuditTrail(id_factory=SequentialIds(), clock=FixedClock(NOW))
        event = trail.build(
            workspace_id=WORKSPACE,
            actor_type=ActorType.USER,
            actor_id=USER,
            action=AuditAction.LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id=USER,
            previous_hash=None,
            metadata={"password": "hunter2", "nested": {"api_key": "sk-1"}, "safe": 1},
        )
        assert event.metadata["password"] == "[redacted]"
        assert event.metadata["nested"]["api_key"] == "[redacted]"
        assert event.metadata["safe"] == 1

    def test_tampering_with_metadata_breaks_the_hash(self) -> None:
        trail = AuditTrail(id_factory=SequentialIds(), clock=FixedClock(NOW))
        event = trail.build(
            workspace_id=WORKSPACE,
            actor_type=ActorType.WORKER,
            actor_id=None,
            action=AuditAction.RELEASE_PUBLISHED,
            subject_type="corpus_release",
            subject_id=uuid4(),
            previous_hash=None,
            metadata={"alias": "current"},
        )
        assert event.verify()
        tampered = event.model_copy(update={"metadata": {"alias": "staging"}})
        assert not tampered.verify()
