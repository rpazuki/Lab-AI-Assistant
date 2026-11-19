# Benchmarks

Status: active
Owner: scientific owner + technical owner
Last verified: 2026-11-19
Source of truth: the JSON files in this directory

Benchmarks are versioned data. Each one records its origin, licence, permitted use,
and sensitivity, and its checksum is stored on every evaluation run — so a moved
goalpost is visible in the run history rather than invisible in a diff.

## Layout

```text
synthetic/    small sets that run offline in CI, over the deterministic fixtures
manifests/    pointers to restricted sets held elsewhere, by reference only
```

`synthetic/` is the only directory with content in the repository. Real gold sets
that contain licensed article text live outside version control; `manifests/`
records where they are, who owns them, and what may be done with them.

## Rules

- **Never overwrite a gold label** without an audit record. A changed label is a
  new benchmark version, and the previous version stays readable.
- **Separate development from blinded test sets.** A set marked `"blinded": true`
  is not used while iterating.
- **Report sample size.** Two cases prove wiring, not quality; the thresholds in
  `synthetic/` are deliberately low because the sets are small.
- **Restricted content never reaches an unapproved provider.** The model-transfer
  policy applies to evaluation exactly as it applies to production.

## Seed keys

A synthetic benchmark names a work by a **seed key** (`work:yeast`), never by a uuid:
the uuid is generated when the fixture corpus is published. `SEED_WORKS` in
`tools/cli/run_benchmarks.py` maps each key to the title the fixture publishes, and a
case naming an unmapped key **fails the run**. Silently skipping it would leave a case
that looks like it passed while measuring nothing.

## Running

```bash
python tools/cli/run_benchmarks.py
```

```bash
python tools/cli/run_benchmarks.py --suite policy
```

```bash
python tools/cli/run_benchmarks.py --list
```

The runner seeds a two-workspace deployment in process — a public work, a work
restricted to a group, an outsider who is a normal member of the same workspace, and a
second workspace — then publishes a release and runs the suites the release gates read.
Exit code is non-zero if any gate fails.

The restricted fixture is restricted through its **rights decision**, not through a
missing grant. The first time this ran, the probe passed because the "restricted" work
was not restricted at all and the outsider had no grants — a probe that proves nothing.
`tests/integration/test_benchmark_runner.py` now asserts both halves: the outsider sees
the public work, and does not see the restricted one.

Release gates read the same files: see
[`docs/evaluation/release-gates.md`](../docs/evaluation/release-gates.md).

## Registering a benchmark with an installation

A real gold set is registered with the installation that serves the corpus, not
committed here: open **Evaluation**, choose **Add benchmark**, and upload the file.
The same shape as `synthetic/`, with two differences:

- `relevant` names papers as `doi:…`, `pmid:…`, `pmcid:…` or a work id, never a seed
  key. Seed keys only mean something to the fixture corpus.
- A registered version cannot change. Register a corrected label as a new `version`.

Record the benchmark's manifest in `manifests/` as usual. See
[the methodology](../docs/evaluation/methodology.md#evaluating-a-release-on-an-installation)
for how runs execute and what their numbers mean.
