"""A corpus containing the cases a portable archive has to survive.

The shared-archive plan asks for a fixture with two releases, a revised paper,
restricted evidence, review history, an empty extraction result and compatible
vectors. Each of those is a case where an exporter can look correct and be
wrong, and none of them appears in a corpus built from one clean paper:

- **Two releases.** A snapshot that can only describe the current state cannot
  resolve a citation into last quarter's answer. Release 1 stays servable after
  release 2 publishes.
- **A revised paper.** The same work, corrected bytes, a new rendition and a new
  digest in release 2 — while the digest release 1 recorded still resolves. An
  exporter that keys evidence by work rather than by revision silently rewrites
  history here (scenario S15).
- **Restricted evidence.** A work whose rights decision names a group. Policy
  has to survive the round trip and be applied by the importer, or a reader
  serves material the publisher restricted (S39).
- **Review history.** One claim accepted, one corrected by a reviewer. The
  correction and the proposal it replaced are both part of the record; an
  exporter that ships only current values loses the reason a number changed.
- **An empty extraction result.** A paper the extractor read and found nothing
  in. In this system that is not an absence of rows: every target abstains with
  ``NOT_REPORTED_AFTER_COMPLETE_SEARCH``, which is a positive statement that the
  source does not report the value. An archive that cannot tell that from "not
  yet extracted" makes every reader pay to re-run it (S12).
- **Compatible vectors.** Embeddings under a named profile, so an importer can
  be tested for reusing them rather than re-embedding (S37).

The fixture builds all of it through the ordinary services. A fixture assembled
by writing rows directly would keep passing after the lifecycle that produces
those rows broke, which is the failure mode it exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext
from lab_assistant.contracts.errors import AbstentionReason
from lab_assistant.contracts.stage import StageCommand, StageName
from lab_assistant.domain.claims import ReviewDecision

__all__ = [
    "ARCHIVE_FIXTURE_CASES",
    "ArchiveFixture",
    "publish_archive_fixture",
    "replicating_rights_policy",
]

#: Titles, chosen so a failure message names the case rather than a number.
MEASURED = "Lipid yield in an engineered strain"
SILENT = "A qualitative survey of oleaginous yeasts"
RESTRICTED = "Unpublished collaborator measurements"
REVISED = "Growth kinetics under nitrogen limitation"

#: What each case is for, so a test that drops one says what it stopped covering.
ARCHIVE_FIXTURE_CASES: dict[str, str] = {
    "two_releases": "a citation into the earlier release still resolves after the later one",
    "revised_paper": "same work, corrected bytes, a new rendition digest in the later release",
    "restricted_evidence": "a rights decision naming a group, which policy must re-apply",
    "review_history": "one accepted claim and one a reviewer corrected",
    "empty_extraction": "a work whose every target abstained after a complete search",
    "compatible_vectors": "embeddings under a named profile, for reuse without re-embedding",
}

#: Bodies. The measured paper carries quotable numbers; the silent one carries
#: none, which is what makes its extraction result empty rather than failed.
_BODIES: dict[str, str] = {
    MEASURED: (
        "Strain YL-7 reached a lipid yield of 0.42 g/g substrate at 30 degrees C "
        "in a 1 L bioreactor on glycerol."
    ),
    SILENT: (
        "We review the literature on oleaginous yeasts and discuss qualitative "
        "trends in substrate preference. No new measurements are reported here."
    ),
    RESTRICTED: (
        "Collaborator measurements report a lipid yield of 0.55 g/g substrate at "
        "28 degrees C, shared under embargo."
    ),
    REVISED: (
        "Growth under nitrogen limitation reached a specific growth rate of "
        "0.18 per hour at 30 degrees C."
    ),
}

#: The corrected text of the revised paper. The number changes, so the rendition
#: digest changes, so release 2 carries a different digest for the same work.
_REVISED_CORRECTION = (
    "Growth under nitrogen limitation reached a specific growth rate of "
    "0.21 per hour at 30 degrees C. This supersedes the value first reported."
)

_TEMPLATE_ID = "yeast-lipid-datasheet"


def replicating_rights_policy():
    """The starter policy with replication of open-licence content granted.

    What an archive test builds its harness with when it means the corpus's
    text to travel. The deployed default does not grant replication — that is
    the data steward's decision — so a harness built without this exercises
    the other half: records travel and the text is withheld.
    """
    from lab_assistant.policy.rights import default_open_access_policy

    return default_open_access_policy(replicate_open_content=True)


def _template_payload() -> dict[str, Any]:
    return {
        "template_id": _TEMPLATE_ID,
        "semantic_version": "1.0.0",
        "title": "Yeast lipid datasheet",
        "scientific_purpose": "Lipid yields and culture conditions per strain.",
        "cardinality": "per_experiment",
        "targets": [
            {
                "target_id": "lipid_yield",
                "predicate": "lipid_yield",
                "object_kind": "quantity",
                "description": "Lipid yield per gram of substrate.",
                "unit_expectation": "g/g",
                "likely_sections": ["Results", "Table"],
            },
            {
                "target_id": "temperature",
                "predicate": "culture_temperature",
                "object_kind": "quantity",
                "description": "Culture temperature.",
                "unit_expectation": "degrees C",
                "likely_sections": ["Methods", "Table"],
            },
        ],
        "wide_projection_columns": ["lipid_yield", "culture_temperature"],
    }


@dataclass(frozen=True)
class ArchiveFixture:
    """What the fixture built, by the name of the case it covers."""

    workspace_id: UUID
    #: Published first, still servable afterwards: the target of an old citation.
    first_release_id: UUID
    #: The successor, carrying the revision and the reviewed claims.
    second_release_id: UUID
    #: What the alias points at once both have published.
    served_release_id: UUID
    work_ids: dict[str, UUID]
    restricted_group_id: UUID
    #: Title -> the rendition digest in each release, in publication order. The
    #: revised paper is the one where these two differ.
    rendition_digests: dict[str, tuple[str, ...]] = field(default_factory=dict)
    accepted_revision_ids: tuple[UUID, ...] = ()
    corrected_revision_ids: tuple[UUID, ...] = ()
    #: The work whose extraction succeeded and reported nothing: every target
    #: abstained after a complete search.
    empty_extraction_work_id: UUID | None = None
    embedding_profile_id: str = "hashing-v1"

    @property
    def release_ids(self) -> tuple[UUID, ...]:
        return (self.first_release_id, self.second_release_id)

    def digest_for(self, title: str, *, release_index: int) -> str:
        return self.rendition_digests[title][release_index]


async def publish_archive_fixture(
    harness: Any,
    actor: ActorContext,
    *,
    name: str = "archive-fixture",
) -> ArchiveFixture:
    """Build and publish the two-release fixture corpus.

    ``actor`` needs the capabilities the ordinary lifecycle needs: managing
    members, scopes, releases and claim review. The harness supplies the
    deterministic clock and ids, so the same call produces the same corpus.
    """
    from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
    from lab_assistant.testing import publish_demo_corpus

    context = harness.context
    workspace_id = actor.workspace_id

    # ── restricted group, before the corpus that references it ──────────────
    group = await harness.accounts.create_group(
        actor,
        slug="embargoed-collaboration",
        display_name="Embargoed collaboration",
        member_user_ids=frozenset({actor.user_id}),
    )

    await _seed_template(harness, workspace_id, actor)

    # ── release 1: four papers, one of them restricted ──────────────────────
    first = await publish_demo_corpus(
        harness,
        actor,
        works=tuple((title, _BODIES[title]) for title in (MEASURED, SILENT, RESTRICTED, REVISED)),
        name=name,
        semantic_version="1.0.0",
        restrict_titles_to_group={RESTRICTED: group.id},
    )

    digests: dict[str, list[str]] = {
        title: [first.rendition_digests[title]] for title in first.rendition_digests
    }

    # ── claims and their review history ─────────────────────────────────────
    accepted, corrected, empty_work_id = await _extract_and_review(
        harness, actor, work_ids=first.work_ids
    )

    # ── the revision: same work, corrected bytes, a new rendition ───────────
    revised_digest = await _revise(harness, actor, work_id=first.work_ids[REVISED])
    digests[REVISED].append(revised_digest)
    for title in digests:
        if title != REVISED:
            digests[title].append(digests[title][0])

    # ── release 2: the successor, with the claim graph ──────────────────────
    releases = ReleaseService(context)
    second, _manifest, _diff = await releases.draft(
        actor,
        ReleaseDraftRequest(
            name=name,
            semantic_version="1.1.0",
            parent_release_id=first.release_id,
            embedding_profile_ids=(harness.settings.embedding_profile,),
            include_claims=True,
            require_claim_review=True,
        ),
    )
    report = await releases.validate(actor, release_id=second.id)
    if not report.passed:
        raise AssertionError(f"the fixture's second release failed its gates: {report.failures}")
    await releases.approve(actor, release_id=second.id)
    await harness.handlers.get(StageName.INDEXING).execute(
        StageCommand(
            stage=StageName.INDEXING,
            workspace_id=workspace_id,
            actor_id=actor.user_id,
            inputs={"release_id": str(second.id)},
        )
    )
    alias = await releases.publish(actor, release_id=second.id)
    assert alias.release_id is not None

    # A reviewer exists only if extraction produced something to review; the
    # fixture asserts that rather than leaving it to the caller to discover.
    if not accepted and not corrected:
        raise AssertionError(
            "the fixture produced no reviewed claims: its review-history case is empty"
        )

    return ArchiveFixture(
        workspace_id=workspace_id,
        first_release_id=first.release_id,
        second_release_id=second.id,
        served_release_id=alias.release_id,
        work_ids=dict(first.work_ids),
        restricted_group_id=group.id,
        rendition_digests={title: tuple(values) for title, values in digests.items()},
        accepted_revision_ids=accepted,
        corrected_revision_ids=corrected,
        empty_extraction_work_id=empty_work_id,
        embedding_profile_id=harness.settings.embedding_profile,
    )


async def _seed_template(harness: Any, workspace_id: UUID, actor: ActorContext) -> None:
    from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion

    async with harness.context.uow_factory() as uow:
        await uow.configs.add_config_version(
            ConfigVersion(
                id=harness.context.new_id(),
                workspace_id=workspace_id,
                kind=ConfigKind.EXTRACTION_TEMPLATE,
                logical_id=_TEMPLATE_ID,
                semantic_version="1.0.0",
                payload=_template_payload(),
                status=ApprovalState.APPROVED,
                created_by=actor.user_id,
            )
        )
        await uow.commit()


async def _extract_and_review(
    harness: Any, actor: ActorContext, *, work_ids: dict[str, UUID]
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], UUID | None]:
    """Extract over every rendition, then accept one claim and correct another.

    The silent paper is extracted too, and that is the point: its result is a
    success with no proposals, which an archive must be able to record as a
    completed stage rather than as work still to do.
    """
    from lab_assistant.application.review import ReviewService

    context = harness.context
    workspace_id = actor.workspace_id

    async with context.uow_factory() as uow:
        renditions = await uow.renditions.list_renditions(workspace_id)

    for rendition in renditions:
        await harness.handlers.get(StageName.EXTRACTION).execute(
            StageCommand(
                stage=StageName.EXTRACTION,
                workspace_id=workspace_id,
                actor_id=actor.user_id,
                inputs={"rendition_id": str(rendition.id), "template": _TEMPLATE_ID},
            )
        )

    silent_work_id = work_ids.get(SILENT)
    empty_work_id = (
        silent_work_id
        if silent_work_id is not None
        and await _every_target_abstained(context, workspace_id, silent_work_id)
        else None
    )

    review = ReviewService(context)
    accepted: list[UUID] = []
    corrected: list[UUID] = []
    for item in await review.queue(actor, include_invalid=True):
        reports_a_value = (
            getattr(item.revision, "abstention_reason", None) is AbstentionReason.REPORTED
        )
        # Correct a claim that reported something. Correcting an abstention
        # would be a reviewer overruling "the paper does not say", which is a
        # different act and not the one this case is about.
        if reports_a_value and not corrected:
            revision, _record = await review.decide(
                actor,
                revision_id=item.revision.id,
                decision=ReviewDecision.CORRECT,
                reason_code="reviewer_corrected_value",
                corrected_value={"value_verbatim": "0.43", "unit": "g/g"},
                comment="transcription error against the table",
            )
            corrected.append(revision.id)
            continue
        revision, _record = await review.decide(
            actor,
            revision_id=item.revision.id,
            decision=ReviewDecision.ACCEPT,
            reason_code="matches_source",
        )
        accepted.append(revision.id)

    return tuple(accepted), tuple(corrected), empty_work_id


async def _every_target_abstained(context: Any, workspace_id: UUID, work_id: UUID) -> bool:
    """True when extraction searched and reported nothing, for every target."""
    async with context.uow_factory() as uow:
        claims = await uow.claims.list_claims(workspace_id, work_id=work_id)
        if not claims:
            return False
        for claim in claims:
            revisions = await uow.claims.list_revisions(workspace_id, claim.id)
            current = next(
                (r for r in revisions if r.id == claim.current_revision_id),
                revisions[-1] if revisions else None,
            )
            if current is None:
                return False
            if (
                getattr(current, "abstention_reason", None)
                is not AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH
            ):
                return False
    return True


async def _revise(harness: Any, actor: ActorContext, *, work_id: UUID) -> str:
    """Correct one paper's bytes, producing a new asset and a new rendition.

    The earlier rendition is not touched. A correction is a successor, and a
    citation into the first release has to keep meaning what it meant.
    """
    from lab_assistant.application.objects import register_object
    from lab_assistant.contracts.canonical import canonical_json
    from lab_assistant.contracts.parsing import (
        NodeKind,
        ParseQualityMetrics,
        RenditionDocument,
        RenditionNode,
    )
    from lab_assistant.domain.assets import Asset, MalwareScanStatus, ValidationStatus
    from lab_assistant.domain.renditions import ParseRun, Rendition
    from lab_assistant.domain.states import ParseState

    context = harness.context
    workspace_id = actor.workspace_id

    document = RenditionDocument(
        format="jats",
        nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=_REVISED_CORRECTION),),
        quality=ParseQualityMetrics(characters=len(_REVISED_CORRECTION), section_count=1),
    )
    rendition_object = await context.object_store.put(
        canonical_json(document.model_dump(mode="json")),
        workspace_id=workspace_id,
        object_class="rendition",
        media_type="application/json",
    )
    asset_object = await context.object_store.put(
        _REVISED_CORRECTION.encode("utf-8"),
        workspace_id=workspace_id,
        object_class="asset",
    )

    async with context.uow_factory() as uow:
        previous = await uow.assets.list_assets(workspace_id, work_id=work_id)
        rights_decision_id = previous[0].rights_decision_id if previous else None

        asset = Asset(
            id=context.new_id(),
            workspace_id=workspace_id,
            sha256=asset_object.sha256,
            byte_length=asset_object.byte_length,
            object_id=asset_object.id,
            media_type_detected="application/xml",
            malware_scan_status=MalwareScanStatus.PASSED,
            validation_status=ValidationStatus.PASSED,
            work_id=work_id,
            rights_decision_id=rights_decision_id,
            identity_verified=True,
            created_at=context.now(),
            metadata={"revision_of": str(previous[0].id) if previous else None},
        )
        await register_object(uow, asset_object)
        await uow.assets.add_asset(asset)

        parse_run = ParseRun(
            id=context.new_id(),
            workspace_id=workspace_id,
            asset_id=asset.id,
            parser_id="jats-etree",
            parser_version="1.0.0",
            input_sha256=asset.sha256,
            run_fingerprint=f"archive_fixture_revision:{work_id}",
            status="succeeded",
        )
        await uow.renditions.add_parse_run(parse_run)
        await register_object(uow, rendition_object)
        await uow.renditions.add_rendition(
            Rendition(
                id=context.new_id(),
                workspace_id=workspace_id,
                parse_run_id=parse_run.id,
                asset_id=asset.id,
                work_id=work_id,
                object_id=rendition_object.id,
                sha256=rendition_object.sha256,
                quality_status=ParseState.READY,
                created_at=context.now(),
            )
        )
        await uow.commit()

    return rendition_object.sha256
