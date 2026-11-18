"""Which completed work may be reused, and which must be done again.

An expensive stage — an extraction, an acquisition, anything that sends a
request to somebody who charges for it — should run once per distinct input.
Deciding whether an input is distinct is a fingerprint's job. Deciding whether
a *recorded outcome* counts as having done the work is this module's, and it is
where the money is.

The failure this exists to fix is recorded as F10, and it is the kind that
costs real money quietly. Extraction looked for reuse by asking whether a claim
revision existed with the right fingerprint. A paper the extractor read and
found nothing in produces no revision — every target abstains, which in this
system is a *positive statement* that the source does not report the value. So
the lookup found nothing, concluded the work had not been done, and called the
model again. On every restart. Forever, for every silent paper in the corpus.

So an outcome is recorded whether or not it produced rows, and reuse is decided
by the outcome rather than by the presence of output:

- **succeeded, empty or truncated** — the work was done. Reuse it. An empty
  result is the one every naive cache gets wrong, and the one this module
  exists for.
- **refused by policy** — a decision, not a failure. The policy version is part
  of the fingerprint, so a refusal recorded under one policy is never reused
  under another; within the same one, asking again gets the same answer at the
  cost of a provider call.
- **failed permanently** — the input is the problem and will still be the
  problem. Reusing it stops an unfixable item being retried forever; what it
  must not do is look like a result, which is why it is reusable *and* carries
  its failure.
- **failed retryably** — not a result, and never a cache hit. A timeout
  recorded as a completed stage is how a transient blip becomes a permanent
  hole in a corpus.
- **outcome unknown** — the request left and its answer did not come back. The
  provider may well have charged for it. Not reusable, because nobody knows
  what it said; worth recording precisely because the cost was real and a
  second attempt doubles it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lab_assistant.contracts.archive import StageOutcome

__all__ = [
    "REUSABLE_OUTCOMES",
    "ReuseDecision",
    "evaluate_reuse",
    "is_reusable",
]

#: Outcomes that count as the work having been done. An allow-list, so an
#: outcome added later is not reused until somebody decides it should be — the
#: safe direction when the cost of being wrong is re-running an extraction
#: versus silently serving a timeout as a result.
REUSABLE_OUTCOMES: frozenset[StageOutcome] = frozenset(
    {
        StageOutcome.SUCCEEDED,
        StageOutcome.SUCCEEDED_EMPTY,
        StageOutcome.SUCCEEDED_TRUNCATED,
        StageOutcome.REFUSED_BY_POLICY,
        StageOutcome.FAILED_PERMANENT,
    }
)


def is_reusable(outcome: StageOutcome) -> bool:
    return outcome in REUSABLE_OUTCOMES


@dataclass(frozen=True, slots=True)
class ReuseDecision:
    """Whether a recorded outcome spares this run, and why."""

    reuse: bool
    outcome: StageOutcome | None = None
    detail: str = ""
    #: True when the recorded outcome is a failure being reused *as* a failure.
    #: The caller must surface it rather than report success: "we know this
    #: fails" and "this succeeded" are different answers.
    carries_failure: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "reuse": self.reuse,
            "outcome": self.outcome.value if self.outcome else None,
            "detail": self.detail,
            "carries_failure": self.carries_failure,
        }


def evaluate_reuse(outcome: StageOutcome | None) -> ReuseDecision:
    """May a run be skipped because this outcome is on record?"""
    if outcome is None:
        return ReuseDecision(reuse=False, detail="no recorded outcome for this input")

    if outcome is StageOutcome.FAILED_RETRYABLE:
        return ReuseDecision(
            reuse=False,
            outcome=outcome,
            detail=(
                "the last attempt failed in a way worth retrying; a timeout recorded as "
                "a completed stage is how a transient blip becomes a permanent hole"
            ),
        )

    if outcome is StageOutcome.OUTCOME_UNKNOWN:
        return ReuseDecision(
            reuse=False,
            outcome=outcome,
            detail=(
                "the previous request left this installation and its answer did not come "
                "back; the provider may have charged for it, and nobody knows what it said"
            ),
        )

    if outcome is StageOutcome.FAILED_PERMANENT:
        return ReuseDecision(
            reuse=True,
            outcome=outcome,
            detail="this input failed permanently before and will fail the same way",
            carries_failure=True,
        )

    if outcome is StageOutcome.SUCCEEDED_EMPTY:
        return ReuseDecision(
            reuse=True,
            outcome=outcome,
            detail=(
                "this input was processed and yielded nothing, which is a result: the "
                "source does not report the value"
            ),
        )

    if outcome is StageOutcome.REFUSED_BY_POLICY:
        return ReuseDecision(
            reuse=True,
            outcome=outcome,
            detail=(
                "policy refused this input under the policy version in the fingerprint; "
                "a different version is a different fingerprint"
            ),
        )

    return ReuseDecision(
        reuse=True, outcome=outcome, detail="this input was processed successfully"
    )
