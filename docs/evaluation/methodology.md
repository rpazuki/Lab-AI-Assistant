Status: active
Owner: technical owner + data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/evaluation/`

# Evaluation methodology

Evaluation gates releases. It is not a report someone reads after publishing.

## Four suites

**Retrieval.** Labelled question → relevant span sets. Reports recall@k, MRR, nDCG.
Measured against **spans**, not works: retrieving the right paper and the wrong
paragraph is a failure the answer will inherit.

**Grounding and citation.** For each generated answer: is every claim supported by a
cited span, does every citation handle resolve, does every quote verify under the
current normalisation version. Reports citation precision, unsupported-handle rate, and
abstention correctness.

**Extraction.** Proposals against reviewer decisions. Reports per-target precision and
recall, unit-error rate, and — separately — **abstention correctness**, because
"not reported" claimed when the value is on page 4 is a different and worse error than
a wrong number.

**Policy.** Probe questions from subjects who must not see certain material. A leak is
a **hard fail** at any threshold. There is no score at which a cross-workspace
disclosure is acceptable.

## Benchmarks

`benchmarks/synthetic/` ships with the repository: retrieval smoke, policy probes, and
generation-citation cases. They are synthetic, offline, and licence-clean, so CI runs
them on every commit with no credentials.

Real lab benchmarks live outside the repository. `benchmarks/manifests/` holds their
manifests — id, digest, licence, owner — so a run names exactly which version it used
without committing material the lab may not redistribute.

## Determinism

Retrieval, fusion, and policy evaluation are deterministic and run with a fixed seed.
Generation is not; generation metrics are reported with the model revision, sampling
settings, and repeat count. A single generation sample is an anecdote.

## Running

```bash
python tools/cli/run_benchmarks.py --suite retrieval
```

Omit `--suite` to run all three. The runner seeds a two-workspace deployment in
process, publishes a release, and exits non-zero if any gate fails, so it is usable as
a CI step rather than a report someone reads. Synthetic cases name works by seed key
rather than uuid — see [`benchmarks/README.md`](../../benchmarks/README.md).

A run produces a report object: suite, benchmark id and digest, release id, embedding
profile, retrieval parameters, per-metric values, and per-case detail. Reports are
stored and referenced by digest from the release manifest, so "which evaluation gated
this release" always has an answer.

## Interpretation

A metric moving without an accompanying change to the corpus, the profile, or the
retrieval parameters means the benchmark drifted — not that the system improved. Cases
are versioned for exactly this reason.

Next: [release gates](release-gates.md)
