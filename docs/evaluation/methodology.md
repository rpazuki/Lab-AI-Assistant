Status: active
Owner: technical owner + data steward
Last verified: 2026-11-19
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

## Evaluating a release on an installation

The synthetic runner above proves wiring. To measure the corpus an installation
actually serves, register a benchmark with it and run it against a release, from the
**Evaluation** page (members with `run_evaluation`: admins and data stewards) or the
API under `/api/v1/evaluation/`. Source: `evaluation/service.py`.

- **Register.** The page takes the benchmark JSON file. A registered `id@version` is
  immutable: the same version with different content is refused, so a corrected gold
  label is a new version and every run's checksum names what it measured against.
  Only `retrieval`, `generation` and `policy` benchmarks can run against a release.
- **Name papers as people know them.** A case's `relevant` list may hold `doi:…`,
  `pmid:…`, `pmcid:…` or a work id. Each is resolved to the work this installation
  holds. One that resolves to nothing stays in the case as a miss and is listed as
  `unresolved_relevant`: a gold paper the corpus lacks is a recall failure, not a
  case to skip.
- **Run.** A run is queued on the `evaluation` stage and completed by the compute
  worker, because a generation run asks the model once per case. It executes as the
  member who requested it, with their grants, and records who that was. A release can
  be measured once it is approved and its projection is indexed.
- **Restricted benchmarks never reach the model.** A `restricted` benchmark runs
  retrieval and policy suites only; a generation run is refused.
- **Policy probes name real accounts.** `user_email` and `workspace_slug` are resolved
  on the installation; a probe whose subject does not exist fails, because it would
  otherwise prove nothing.

What a generation run's two scores mean:

- **`citation_validity`** is the share of the handles an answer cited that were among
  the handles it was given. An invented handle lowers it in proportion; the
  `generation:no_unsupported_citations` gate fails on any.
- **`faithfulness_proxy`** is the share of the numbers in the answer that appear in
  the evidence the answer *cited*. A figure stated without a citation is checked
  against nothing and counts as unsupported. Handle digits are not numbers. Each case
  lists its `unsupported_numbers`, so a failure points at the figure. It is a cheap
  mechanical check, not a judge of meaning: a sentence with no numbers scores 1.0.

One limit to know when reading the numbers: **a run does not gate publication yet.**
Indexing happens after approval, so a run can only exist between approval and publish;
requiring a passing run there is a release-policy decision that has not been made.
Today the result informs the approver, who decides.

## Interpretation

A metric moving without an accompanying change to the corpus, the profile, or the
retrieval parameters means the benchmark drifted — not that the system improved. Cases
are versioned for exactly this reason.

Next: [release gates](release-gates.md)
