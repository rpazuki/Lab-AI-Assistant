"""What a policy checkpoint means, and who may issue one (phase 2).

Three questions, answered here so that the publisher that writes a checkpoint
and the reader that applies it cannot disagree about any of them.

**May this writer issue it?** The policy head is a control record like the
catalog head, fenced the same way, and a writer checks *both* heads before
moving either (`domain/publication.authority_refusal`). A handover moves the
two one after the other and can be interrupted between them; whichever moved
first fences the old writer from the pair.

**May a reader rely on it?** Not if it belongs to another library, goes
backwards from a sequence the reader already applied (a replay), names the
same sequence as a different checkpoint (a fork), or is older than the
checkpoint the snapshot was built under. And not for ever: restricted material
may be served on its word until `valid_until`, unrestricted material until
`public_valid_until`, and nothing after that. A reader that cannot refresh
policy stops disclosing restricted material at a bound it can name, rather than
continuing indefinitely on the last thing it heard (scenario S20).

**What does it do to a snapshot?** It narrows, and it never widens. The rights
a reader applies are the *intersection* of the decision the snapshot froze and
the standing the checkpoint reports: the stricter status, the actions both
allow, every restriction either names, the groups both admit, the earlier
expiry. A decision the checkpoint does not mention is withheld. So a stale or
replayed checkpoint can be out of date about a revocation and can never make a
reader more permissive than the snapshot the publisher signed — and a reader
that rolls back to an older release still applies today's revocations
(scenario S19), because the overlay is applied to whatever snapshot is active.

The cost is stated rather than hidden: an embargo that lifts, or a review date
a steward renews, is a widening, and a widening is a *publication*. It reaches
readers with the next snapshot, not with the next checkpoint. That is the safe
direction to be slow in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from lab_assistant.contracts.archive import ArtifactClass, ArtifactRef, UnavailableReason
from lab_assistant.contracts.archive_catalog import (
    AssetRecord,
    RenditionRecord,
    RightsDecisionRecord,
)
from lab_assistant.contracts.archive_policy import PolicyCheckpoint, RightsStanding
from lab_assistant.domain.publication import CommitVerdict, authority_refusal
from lab_assistant.domain.states import ReleaseState, RightsState

__all__ = [
    "CheckpointStatus",
    "CheckpointVerdict",
    "EffectiveRights",
    "PolicyOverlay",
    "evaluate_checkpoint",
    "evaluate_policy_commit",
    "narrow",
    "overlay",
    "validity_window",
]


# ── issuing ────────────────────────────────────────────────────────────────


def evaluate_policy_commit(
    policy_head,
    *,
    catalog_head,
    library_id: UUID,
    owner_installation_id: UUID,
    writer_epoch: int,
) -> CommitVerdict:
    """May this writer move the policy head, and to what sequence?

    ``writer_epoch`` is the epoch the writer started with, for the reason
    `evaluate_commit` gives: a function that re-read it from a head would
    permit the writer a handover just fenced.
    """
    for name, record in (("policy head", policy_head), ("catalog head", catalog_head)):
        refused = authority_refusal(
            record,
            library_id=library_id,
            owner_installation_id=owner_installation_id,
            writer_epoch=writer_epoch,
        )
        if refused is not None:
            return CommitVerdict(
                permitted=False,
                refusal=refused.refusal,
                detail=f"the {name} refuses this writer: {refused.detail}",
            )
    if policy_head is None:
        return CommitVerdict(permitted=True, commit_sequence=1)
    return CommitVerdict(permitted=True, commit_sequence=policy_head.policy_sequence + 1)


def validity_window(
    issued_at: datetime,
    *,
    validity: timedelta,
    public_validity: timedelta | None = None,
) -> tuple[datetime, datetime]:
    """``(valid_until, public_valid_until)`` for a checkpoint issued now.

    The public window is never shorter than the restricted one: material with
    no restriction is never held to a tighter bound than material with one. It
    is longer only when somebody approved it, which is what passing
    ``public_validity`` means.
    """
    if validity <= timedelta(0):
        raise ValueError("a checkpoint's validity must be positive")
    valid_until = issued_at + validity
    public = issued_at + max(validity, public_validity or timedelta(0))
    return valid_until, public


# ── relying ────────────────────────────────────────────────────────────────


class CheckpointStatus(StrEnum):
    """What a checkpoint is worth to a reader, now."""

    #: Fresh, and the reader may rely on it for everything it governs.
    CURRENT = "current"
    #: Past `valid_until` and inside the approved public window: unrestricted
    #: material may still be served on its word and restricted material may not.
    RESTRICTED_EXPIRED = "restricted_expired"
    #: Past `public_valid_until` as well. Nothing it governs may be served
    #: until a newer checkpoint arrives.
    EXPIRED = "expired"
    #: A lower sequence than one this reader already applied: a replayed or
    #: rolled-back head. Applying it would bring revoked permissions back.
    REPLAYED = "replayed"
    #: The same sequence as a checkpoint this reader already holds, with a
    #: different digest. Two authorities, or one that rewrote history.
    FORKED = "forked"
    #: Older than the checkpoint the snapshot was built under. The snapshot
    #: knows of policy this checkpoint does not.
    BEHIND_SNAPSHOT = "behind_snapshot"
    FOREIGN_LIBRARY = "foreign_library"
    #: Signed content that contradicts itself — a window that ends before it
    #: starts, a successor with no parent. A publisher never writes one.
    MALFORMED = "malformed"


_REFUSED = frozenset(
    {
        CheckpointStatus.REPLAYED,
        CheckpointStatus.FORKED,
        CheckpointStatus.BEHIND_SNAPSHOT,
        CheckpointStatus.FOREIGN_LIBRARY,
        CheckpointStatus.MALFORMED,
    }
)


@dataclass(frozen=True, slots=True)
class CheckpointVerdict:
    status: CheckpointStatus
    detail: str

    @property
    def refused(self) -> bool:
        """Must not be applied at all: keep whatever was applied before."""
        return self.status in _REFUSED

    def serves(self, *, public: bool) -> bool:
        """May material of this class be served on this checkpoint's word?"""
        if self.status is CheckpointStatus.CURRENT:
            return True
        if self.status is CheckpointStatus.RESTRICTED_EXPIRED:
            return public
        return False

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "detail": self.detail}


def evaluate_checkpoint(
    checkpoint: PolicyCheckpoint,
    *,
    library_id: UUID,
    now: datetime,
    last_seen_sequence: int | None = None,
    last_seen_digest: str | None = None,
    minimum_sequence: int = 0,
    minimum_digest: str | None = None,
) -> CheckpointVerdict:
    """May a reader rely on this checkpoint, and for what?

    ``last_seen_*`` is the checkpoint the reader applied most recently, which
    it records durably precisely so that this check survives a restart.
    ``minimum_*`` comes from the snapshot being served: the checkpoint that was
    current when it was built. Signature trust is a separate question,
    answered by `domain/signing.py` before this one is asked.
    """
    digest = checkpoint.signing_digest()

    if checkpoint.library_id != library_id:
        return CheckpointVerdict(
            CheckpointStatus.FOREIGN_LIBRARY,
            f"this checkpoint governs library {checkpoint.library_id}, not {library_id}",
        )

    malformed = _malformation(checkpoint)
    if malformed is not None:
        return CheckpointVerdict(CheckpointStatus.MALFORMED, malformed)

    if last_seen_sequence is not None:
        if checkpoint.policy_sequence < last_seen_sequence:
            return CheckpointVerdict(
                CheckpointStatus.REPLAYED,
                f"policy sequence {checkpoint.policy_sequence} is behind "
                f"{last_seen_sequence}, which this reader already applied: a replayed or "
                "rolled-back head. Applying it would restore what a later checkpoint "
                "revoked",
            )
        if (
            checkpoint.policy_sequence == last_seen_sequence
            and last_seen_digest is not None
            and digest != last_seen_digest
        ):
            return CheckpointVerdict(
                CheckpointStatus.FORKED,
                f"two different checkpoints claim policy sequence {last_seen_sequence}",
            )

    if checkpoint.policy_sequence < minimum_sequence:
        return CheckpointVerdict(
            CheckpointStatus.BEHIND_SNAPSHOT,
            f"policy sequence {checkpoint.policy_sequence} is older than "
            f"{minimum_sequence}, which the snapshot was built under: fetch that one "
            "or a later one",
        )
    if (
        minimum_sequence
        and checkpoint.policy_sequence == minimum_sequence
        and minimum_digest is not None
        and digest != minimum_digest
    ):
        return CheckpointVerdict(
            CheckpointStatus.FORKED,
            f"the snapshot names a different checkpoint at policy sequence {minimum_sequence}",
        )

    if now >= checkpoint.public_valid_until:
        return CheckpointVerdict(
            CheckpointStatus.EXPIRED,
            f"valid until {checkpoint.public_valid_until.isoformat()}; nothing it governs "
            "may be served until a newer checkpoint arrives",
        )
    if now >= checkpoint.valid_until:
        return CheckpointVerdict(
            CheckpointStatus.RESTRICTED_EXPIRED,
            f"restricted material lapsed at {checkpoint.valid_until.isoformat()}; "
            "unrestricted material may be served until "
            f"{checkpoint.public_valid_until.isoformat()}",
        )
    return CheckpointVerdict(
        CheckpointStatus.CURRENT,
        f"current until {checkpoint.valid_until.isoformat()}",
    )


def _malformation(checkpoint: PolicyCheckpoint) -> str | None:
    if checkpoint.valid_until <= checkpoint.issued_at:
        return "the checkpoint's validity ends before it begins"
    if checkpoint.public_valid_until < checkpoint.valid_until:
        return (
            "unrestricted material is held to a shorter bound than restricted material, "
            "which no publisher writes"
        )
    if checkpoint.policy_sequence == 1 and checkpoint.parent_checkpoint_digest is not None:
        return "the first checkpoint of a library names a parent"
    if checkpoint.policy_sequence > 1 and checkpoint.parent_checkpoint_digest is None:
        return (
            f"policy sequence {checkpoint.policy_sequence} names no parent, so nothing "
            "connects it to the checkpoints before it"
        )
    return None


# ── narrowing ──────────────────────────────────────────────────────────────

#: Least strict first. A status a reader does not recognise ranks above all of
#: these: a newer producer's state must not read as a permission to an older
#: reader.
_STRICTNESS: dict[str, int] = {
    RightsState.PERMITTED.value: 0,
    RightsState.RESTRICTED.value: 1,
    RightsState.EXPIRED.value: 2,
    RightsState.UNKNOWN.value: 3,
    RightsState.PROHIBITED.value: 4,
}
_UNRECOGNISED = max(_STRICTNESS.values()) + 1


def _rank(status: str | None) -> int:
    return _STRICTNESS.get(status or RightsState.UNKNOWN.value, _UNRECOGNISED)


@dataclass(frozen=True, slots=True)
class EffectiveRights:
    """A rights decision after the checkpoint has narrowed it."""

    decision_id: UUID
    status: str
    allowed_actions: frozenset[str]
    restrictions: tuple[str, ...]
    restricted_to_group_ids: tuple[UUID, ...]
    review_due_at: datetime | None
    #: What the checkpoint took away, by name. Empty when it changed nothing.
    narrowed_by: tuple[str, ...] = ()

    @property
    def is_public(self) -> bool:
        """Unrestricted material: the class an approved offline window covers."""
        return (
            self.status == RightsState.PERMITTED.value
            and not self.restricted_to_group_ids
            and not self.restrictions
        )

    def withheld(self, *, now: datetime) -> UnavailableReason | None:
        """Why material under this decision may not be served now, if it may not."""
        if self.status == RightsState.EXPIRED.value:
            return UnavailableReason.EXPIRED
        if self.status not in (RightsState.PERMITTED.value, RightsState.RESTRICTED.value):
            return UnavailableReason.WITHHELD_BY_RIGHTS
        if self.review_due_at is not None and now >= self.review_due_at:
            return UnavailableReason.EXPIRED
        return None

    def permits(self, action: str, *, now: datetime) -> bool:
        return self.withheld(now=now) is None and action in self.allowed_actions


def narrow(frozen: RightsDecisionRecord, standing: RightsStanding | None) -> EffectiveRights:
    """The decision a snapshot froze, narrowed by what the checkpoint says now.

    Intersection, field by field, so the result is never more permissive than
    either input. That property is the whole design: it is what lets a reader
    apply a checkpoint it cannot fully trust to be the latest, because the
    worst a stale one can do is withhold less than a newer one would — never
    grant more than the publisher signed.
    """
    if standing is None:
        # Absent is denied. "The policy said nothing about this" is not a grant,
        # and inheriting the snapshot's permission here would make deleting an
        # entry from the checkpoint the easiest way to widen access.
        return EffectiveRights(
            decision_id=frozen.record_id,
            status=RightsState.PROHIBITED.value,
            allowed_actions=frozenset(),
            restrictions=tuple(frozen.restrictions),
            restricted_to_group_ids=tuple(frozen.restricted_to_group_ids),
            review_due_at=frozen.review_due_at,
            narrowed_by=("absent_from_policy",),
        )

    reasons: list[str] = []

    if _rank(standing.status) > _rank(frozen.status):
        status = standing.status
        reasons.append("revoked" if status == RightsState.PROHIBITED.value else "status")
    else:
        status = frozen.status or RightsState.UNKNOWN.value
    if _rank(status) == _UNRECOGNISED:
        reasons.append(f"unrecognised_status:{status}")
        status = RightsState.PROHIBITED.value

    allowed = frozenset(frozen.allowed_actions) & frozenset(standing.allowed_actions)
    if allowed != frozenset(frozen.allowed_actions):
        reasons.append("actions_withdrawn")

    restrictions = tuple(dict.fromkeys((*frozen.restrictions, *standing.restrictions)))
    if len(restrictions) != len(tuple(dict.fromkeys(frozen.restrictions))):
        reasons.append("restrictions_added")

    groups = _narrow_groups(
        tuple(frozen.restricted_to_group_ids), tuple(standing.restricted_to_group_ids)
    )
    if groups is None:
        # Restricted to two sets of groups with nobody in both: nobody
        # qualifies, which is a prohibition however it is spelled.
        groups = tuple(frozen.restricted_to_group_ids)
        status = RightsState.PROHIBITED.value
        reasons.append("groups_disjoint")
    elif groups != tuple(frozen.restricted_to_group_ids):
        reasons.append("groups_narrowed")

    review_due_at = _earliest(frozen.review_due_at, standing.review_due_at)
    if review_due_at != frozen.review_due_at:
        reasons.append("expiry_brought_forward")

    return EffectiveRights(
        decision_id=frozen.record_id,
        status=status,
        allowed_actions=allowed,
        restrictions=restrictions,
        restricted_to_group_ids=groups,
        review_due_at=review_due_at,
        narrowed_by=tuple(reasons),
    )


def _narrow_groups(frozen: tuple[UUID, ...], current: tuple[UUID, ...]) -> tuple[UUID, ...] | None:
    """The groups both admit. Empty means "the workspace default", not "nobody".

    ``None`` when both restrict and nobody is in both.
    """
    if not current:
        return frozen
    if not frozen:
        return current
    admitted = set(current)
    both = tuple(group for group in frozen if group in admitted)
    return both or None


def _earliest(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


# ── a snapshot under a checkpoint ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyOverlay:
    """What a checkpoint withholds from one snapshot, and why.

    Per record and per artifact, because the two answer different questions.
    A *record* is withheld when the decision governing that acquisition
    withholds it. An *artifact* — the bytes — is withheld only when every
    acquisition that uses those bytes withholds them: identical bytes acquired
    twice under different rights are one payload and two decisions (scenario
    S14), and revoking one must not take the other's evidence with it (S40).
    """

    verdict: CheckpointVerdict
    effective: Mapping[UUID, EffectiveRights] = field(default_factory=dict)
    withheld_assets: Mapping[UUID, UnavailableReason] = field(default_factory=dict)
    withheld_renditions: Mapping[UUID, UnavailableReason] = field(default_factory=dict)
    withheld_artifacts: Mapping[UUID, UnavailableReason] = field(default_factory=dict)
    withdrawn_releases: frozenset[UUID] = frozenset()
    retracted_works: frozenset[UUID] = frozenset()
    #: Decisions the snapshot carries and the checkpoint does not mention.
    absent_decisions: frozenset[UUID] = frozenset()

    def counts(self) -> dict[str, int]:
        return {
            "policy_withheld_asset": len(self.withheld_assets),
            "policy_withheld_rendition": len(self.withheld_renditions),
            "policy_withheld_artifact": len(self.withheld_artifacts),
            "policy_withdrawn_release": len(self.withdrawn_releases),
            "policy_retracted_work": len(self.retracted_works),
            "policy_absent_decision": len(self.absent_decisions),
            "policy_narrowed_decision": sum(
                1 for rights in self.effective.values() if rights.narrowed_by
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.as_dict(),
            "counts": self.counts(),
            "narrowed": {
                str(decision_id): list(rights.narrowed_by)
                for decision_id, rights in sorted(self.effective.items(), key=lambda kv: str(kv[0]))
                if rights.narrowed_by
            },
        }


def overlay(
    checkpoint: PolicyCheckpoint,
    *,
    verdict: CheckpointVerdict,
    now: datetime,
    rights: Mapping[UUID, RightsDecisionRecord],
    assets: Mapping[UUID, AssetRecord],
    renditions: Mapping[UUID, RenditionRecord],
    inventory: Mapping[UUID, ArtifactRef],
    release_ids: Iterable[UUID] = (),
) -> PolicyOverlay:
    """Apply a checkpoint to a snapshot's records, without a database.

    A refused verdict yields an empty overlay: a checkpoint that must not be
    relied on must not be relied on to withhold either, and the caller keeps
    whatever it applied before.
    """
    if verdict.refused:
        return PolicyOverlay(verdict=verdict)

    standings = {standing.decision_id: standing for standing in checkpoint.rights}
    effective = {
        decision_id: narrow(record, standings.get(decision_id))
        for decision_id, record in rights.items()
    }

    def decision_withholds(decision_id: UUID | None) -> UnavailableReason | None:
        if decision_id is None:
            # Never inferred as a grant: bytes nothing permits are bytes
            # nothing permits.
            return UnavailableReason.WITHHELD_BY_RIGHTS
        decided = effective.get(decision_id)
        if decided is None:
            return UnavailableReason.WITHHELD_BY_RIGHTS
        reason = decided.withheld(now=now)
        if reason is not None:
            return reason
        if not verdict.serves(public=decided.is_public):
            # The checkpoint is too old to vouch for this class of material.
            return UnavailableReason.EXPIRED
        return None

    tombstoned = {entry.sha256 for entry in checkpoint.tombstones}

    withheld_assets: dict[UUID, UnavailableReason] = {}
    for asset_id, asset in assets.items():
        if asset.sha256 in tombstoned:
            withheld_assets[asset_id] = UnavailableReason.TOMBSTONED
            continue
        reason = decision_withholds(asset.rights_decision_id)
        if reason is not None:
            withheld_assets[asset_id] = reason

    withheld_renditions: dict[UUID, UnavailableReason] = {}
    for rendition_id, rendition in renditions.items():
        if rendition.sha256 in tombstoned:
            withheld_renditions[rendition_id] = UnavailableReason.TOMBSTONED
            continue
        source = assets.get(rendition.asset_id)
        if source is None:
            withheld_renditions[rendition_id] = UnavailableReason.MISSING
            continue
        # A rendition is its asset's text: whatever withholds the asset
        # withholds what was extracted from it.
        reason = withheld_assets.get(rendition.asset_id)
        if reason is not None:
            withheld_renditions[rendition_id] = reason

    withheld_artifacts = _withheld_artifacts(
        inventory, decision_withholds=decision_withholds, tombstoned=tombstoned
    )

    releases = {standing.release_id: standing for standing in checkpoint.releases}
    withdrawn = frozenset(
        release_id
        for release_id in release_ids
        if (standing := releases.get(release_id)) is not None
        and standing.status == ReleaseState.WITHDRAWN.value
    )
    retracted = frozenset(
        standing.work_id
        for standing in checkpoint.works
        if standing.retraction_status == "retracted"
    )

    return PolicyOverlay(
        verdict=verdict,
        effective=effective,
        withheld_assets=withheld_assets,
        withheld_renditions=withheld_renditions,
        withheld_artifacts=withheld_artifacts,
        withdrawn_releases=withdrawn,
        retracted_works=retracted,
        absent_decisions=frozenset(set(rights) - set(standings)),
    )


def _withheld_artifacts(
    inventory: Mapping[UUID, ArtifactRef],
    *,
    decision_withholds,
    tombstoned: set[str],
) -> dict[UUID, UnavailableReason]:
    """Bytes no acquisition may still use, following the retention edges.

    An artifact governed by several decisions is withheld only when all of
    them withhold it. One that depends on a withheld artifact is withheld with
    it: a rendition is its asset's text.
    """
    decided: dict[UUID, UnavailableReason | None] = {}

    def resolve(artifact_id: UUID, path: frozenset[UUID]) -> UnavailableReason | None:
        if artifact_id in decided:
            return decided[artifact_id]
        ref = inventory.get(artifact_id)
        if ref is None:
            return UnavailableReason.MISSING
        if artifact_id in path:  # a cycle is a malformed inventory, not a grant
            return UnavailableReason.MISSING

        reason: UnavailableReason | None = None
        if ref.sha256 in tombstoned:
            reason = UnavailableReason.TOMBSTONED
        elif ref.governed_by:
            reasons = [decision_withholds(decision_id) for decision_id in ref.governed_by]
            if all(item is not None for item in reasons):
                reason = (
                    UnavailableReason.WITHHELD_BY_RIGHTS
                    if UnavailableReason.WITHHELD_BY_RIGHTS in reasons
                    else reasons[0]
                )
        elif ref.artifact_class in (ArtifactClass.ASSET, ArtifactClass.RENDITION):
            # Content bytes that no decision governs are not servable by default.
            reason = UnavailableReason.WITHHELD_BY_RIGHTS

        if reason is None:
            for dependency in ref.depends_on:
                inherited = resolve(dependency, path | {artifact_id})
                if inherited is not None:
                    reason = inherited
                    break

        decided[artifact_id] = reason
        return reason

    return {
        artifact_id: reason
        for artifact_id in sorted(inventory, key=str)
        if (reason := resolve(artifact_id, frozenset())) is not None
    }
