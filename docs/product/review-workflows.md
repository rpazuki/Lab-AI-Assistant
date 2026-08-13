Status: active
Owner: reviewer + data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/review.py`, `extraction.py`

# Review workflows

Extraction proposes. Review decides. Nothing unreviewed reaches a release.

## What arrives in the queue

A proposal carries: the value, its unit, the target it answers, the evidence span it
came from, the confidence, and the parse quality of the rendition beneath it. It is
already **validated** — a proposal whose quote does not match its span, whose unit is
not in the template's allowed set, or whose number will not parse never reaches a
human. Reviewer attention is the scarcest resource in the system and is not spent on
mechanical faults.

## The three actions

**Accept.** The claim becomes reviewed, with the reviewer, timestamp, and revision
recorded.

**Correct.** You supply the right value. The original proposal is kept as a prior
revision — the disagreement is the useful signal, and overwriting it destroys the only
data that would improve extraction.

**Reject.** With a reason. Reasons aggregate into the evaluation suite, which is how a
systematically bad template gets fixed rather than endlessly patched by hand.

## Span verification

Every claim points at an evidence span. Before it can enter a release, the span's
quote must still match the rendition **under the current normalisation version** — not
byte-for-byte. Publishers change µ to μ, insert soft hyphens, and re-flow ligatures;
an exact-bytes gate would fail on all of it and teach everyone to bypass the check.
The rule and its exact transformations are in
[renditions and spans](../data-contracts/renditions-and-spans.md).

If a document is re-parsed with a better parser, spans are re-anchored and anything
that cannot be re-anchored is flagged for review rather than dropped.

## Conflicts

Two claims can disagree legitimately — different strains, different conditions. A
conflict is only recorded when the claims are **comparable**: same target, compatible
units, overlapping conditions. Comparability is a domain rule, not a reviewer's
judgement call each time.

## Batches

Review one target across many works at once. A reviewer building the same mental model
repeatedly is slower and less consistent than one holding it for twenty papers.

## The audit view

Every review action is an audit row: who, what, when, which revision, which policy
version. A steward can reconstruct how any published number came to be trusted. That
is the whole point.
