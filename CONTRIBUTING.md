# Contributing

## Local environment

| Tool | Version |
|---|---|
| Python | 3.12 (3.13 is not supported) |
| Node.js | 22+ |
| Docker | 24+ |

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e "packages/python[dev,parsers]" -e services/api
cd apps/web && npm install
pre-commit install
```

## Test levels, and which one you owe

| Level | Location | Runs against | Write one when |
|---|---|---|---|
| Unit | `packages/python/tests/` | pure functions, invariants | you add a rule, a state machine, or a fingerprint |
| Contract | `tests/contract/` | adapters and layer boundaries | you add an adapter, a connector, or a provider |
| Integration | `tests/integration/` | in-memory and SQLite stacks | you change orchestration, projections, or SQL |
| Policy | `tests/policy/` | two workspaces, adversarially | you touch anything that filters or counts |
| End to end | `tests/e2e/` | the whole lifecycle | you change the lifecycle's shape |
| API | `services/api/tests/` | real routes over the deterministic stack | you add or change a route |
| Web | `apps/web/tests/` | components and the API client | you change the interface |

```bash
.venv/bin/pytest -q                                  # everything, offline
.venv/bin/pytest -q tests/policy                     # isolation only
LAB_TEST_DATABASE_URL=postgresql+asyncpg://... .venv/bin/pytest -q tests/integration
LAB_TEST_S3_BUCKET=scratch LAB_TEST_S3_ACCESS_KEY=... LAB_TEST_S3_SECRET_KEY=... \
  .venv/bin/pytest -q tests/integration/test_s3_live_endpoint.py   # a real bucket
cd apps/web && npm test -- --run
```

The suite is **offline by construction**: fixture-keyed fakes, in-memory adapters,
and SQLite. A test that reads a shared checkpoint directory, a home directory, real
credentials, or the network is a bug in the test — the predecessor had one that
failed because it read a production PubMed checkpoint.

## Fixtures and fakes

- A fake **raises on an unmatched request**. Never add a fallback default: a
  permissive fake makes an end-to-end suite pass while proving nothing.
- Fixture provenance is recorded. A fixture derived from a real paper needs its
  licence and a redaction statement; synthetic is preferred.
- Live connector tests are opt-in (`LAB_LIVE_SOURCE_TESTS=1`), low volume, and
  never required for an ordinary run.

## Adding things

**A connector.** Implement `SourceConnector`, declare a capability manifest, add
recorded fixtures, map upstream failures onto `ErrorClass`, write the connector
documentation page with an owner and a terms review date, and state which existing
reconciliation rules the new source violates. Contract tests walk every registered
connector, so a missing capability fails CI.

**A parser.** Implement `DocumentParser`, add a profile with *achievable*
thresholds, and add gold fixtures — including the hard cases for that format. If a
threshold is not achievable, change the threshold in writing with evidence; do not
quietly lower a gate.

**An extraction template.** It is workspace data, not code: a YAML file under
`configs/examples/templates/` with a semantic version. Changing meaning or
cardinality is a major version. Document it under `docs/extraction-templates/`.

**A provider.** Implement the protocol, declare `ProviderProperties` honestly
(retention, training use, regions, accepted sampling parameters), and add contract
tests for refusal and for truncation.

**A migration.** Add the table to `adapters/db/tables.py`, autogenerate the
revision, and write the data-migration and rollback notes in the docstring. CI runs
`alembic check` so a hand-written migration cannot drift from the tables.

## Definition of done

A lifecycle change is done when it has:

- a typed contract and an outcome taxonomy;
- domain invariants and an authorisation check;
- idempotency and retry semantics;
- provenance or audit events wherever a decision happens;
- unit, contract, and integration tests;
- user *and* operator documentation;
- migration and backward-compatibility notes where applicable;
- a rights and threat review if it moves data anywhere new;
- no unexplained config or generated-schema drift.

## Branches, commits, and review

Branch from `main`. Keep a pull request to one reviewable change. The PR template
carries the checklist, and CODEOWNERS routes the review perspectives — combined
people are fine, absent perspectives are not.

A changed durable decision is a **new superseding ADR**, never an edit to an
accepted one.

## Performance and cost

Every external call costs someone's rate limit or budget. Before adding one, check
whether an existing fingerprint already makes the work cacheable, and whether a
bulk or delta route exists. Release builds must stay proportional to their diff;
`tests/integration/test_incremental_projection.py` proves it and will fail if a
change makes a build proportional to corpus size instead.
