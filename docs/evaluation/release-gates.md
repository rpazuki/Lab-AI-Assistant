Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/release.py`

# Release gates

A release cannot be published until an evaluation run over **that release** clears its
gates. The report's digest is recorded in the manifest.

## Gate structure

| Suite | Gate kind | Default posture |
|---|---|---|
| policy probes | hard | any leak fails, unconditionally |
| citation resolution | hard | every handle in every sampled answer must resolve |
| span verification | hard | every included span verifies under the current normalisation version |
| retrieval recall@10 | threshold | set per collection; not lower than the previous published release without a recorded reason |
| citation precision | threshold | per collection |
| extraction precision, abstention correctness | threshold | per template |
| answer latency p95 | threshold | operational, not scientific |

**Hard gates have no threshold and no override.** A policy leak, an unresolvable
citation, or an unverifiable span is a defect, not a number to negotiate.

Thresholds are **per collection**, stored in the scope version. A methods-heavy corpus
and an abstract-only corpus should not share a recall target.

## Regression rule

A threshold gate compares against the previous published release as well as its
absolute floor. A drop is a blocker unless a steward records a reason — usually "the
corpus grew into harder material", which is legitimate and should be written down.

## Who decides

The steward approving a release. The gate makes the numbers unavoidable; it does not
replace the judgement. A gate that can be silently bypassed is decoration, and one that
cannot be consciously overridden with a recorded reason gets bypassed by other means.

## When a gate fails

`publish_blockers` names every failure at once, with the metric, the observed value,
the threshold, and the report digest. Fix the corpus, the template, or the parameters —
then re-run evaluation and re-validate. Editing the threshold is a recorded change to
the scope version, visible in the release manifest, not a quiet fix.
