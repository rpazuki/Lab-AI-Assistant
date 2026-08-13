# AGENTS.md

The repository's entry point for AI agents and new contributors. It is short on
purpose: this file points at the authority for each fact, it does not restate it.
If something here disagrees with an ADR or a data contract, the ADR or contract
wins and this file is wrong.

## What this is

An internal research-evidence platform for a scientific lab. No public access, no
self-registration, no anonymous query endpoint. The central object is a versioned,
policy-controlled evidence graph; **ingestion means only promoting an approved
corpus release into serving indexes**.

## Required reading before changing anything

1. [Documentation index](docs/README.md) — audiences and authoritative pages
2. [Architecture](docs/architecture/system-context.md) and
   [module boundaries](docs/architecture/modules-and-dependencies.md)
3. [Data contracts](docs/data-contracts/terminology.md) — the vocabulary is precise
4. [ADRs](docs/architecture/adr/) — durable decisions and their reasoning
5. [CONTRIBUTING.md](CONTRIBUTING.md) — definition of done

## Dependency rules

Enforced by `tests/contract/test_module_dependencies.py`, not by convention:

```text
contracts            -> standard library + pydantic
domain               -> contracts
policy, provenance   -> contracts, domain
application          -> contracts, domain, policy, provenance
adapters             -> application ports + external SDKs
web                  -> the typed API client only
```

Forbidden, and each has a test: domain importing SQLAlchemy, FastAPI, an SDK, the
clock, or the filesystem; connectors writing tables; parsers creating chunks;
extraction publishing a release; a route containing pipeline logic; the frontend
reimplementing a rights or lifecycle rule; workers importing each other.

## Invariants you must not break

These are constraints, tests, or gates — never comments:

- an automatic merge needs an exact **strong** identifier bridge; title similarity
  proposes a review and can never merge (`domain/identity.py`);
- a contradiction between strong identifiers is a **conflict**, not last-write-wins;
- no asset enters parsing without a recorded rights decision permitting local
  processing (`domain/assets.py`);
- every accepted claim's evidence resolves in its rendition under the versioned
  normalisation contract (`domain/normalisation.py`, ADR-0010);
- retrieval filters by policy **before** scoring and again after ranking — inside the
  candidate SQL, for structured claims as well as evidence chunks — and counts follow
  the same filter. How much is withheld is a steward's number, not every member's;
- a `limit` is a page size, never a corpus-completeness control: a lifecycle path that
  must see every row pages with a keyset cursor (`application/pagination.py`);
- a durable task carries the actor who asked for it. There is no fallback to the
  workspace id, because a worker audit row naming the workspace is wrong and looks
  fine;
- bytes deduplicate, acquisitions do not: an asset row is unique per (workspace,
  digest, work, manifestation), and each acquisition keeps its own rights decision;
- every external source request acquires a slot from the stored rate budget
  (ADR-0018) — `discover` through `throttle`, `acquire`/`resolve` through `request`; a
  declared limit nobody acquires against is documentation, and an AST test forbids a
  direct connector call from the application layer;
- a retryable failure returns `StageStatus.FAILED` with its error class, never
  `BLOCKED`. `BLOCKED` means "waiting on a dependency" and is not claimable, so a
  retryable stage that returns it leaves the work stuck while looking like
  back-pressure;
- an unauthenticated endpoint that verifies a secret is rate-limited by a
  **store-backed** bucket keyed by digest (ADR-0016): argon2id makes each guess
  expensive for the server too;
- a projection is joined on its full key, `(workspace_id, release_id, chunk_key)`:
  copied chunks share keys across releases by design;
- a published release is immutable; a correction is a successor release;
- a citation handle the server did not supply produces a **flagged answer**, never a
  500 and never a silent drop;
- every cache and idempotency key carries workspace and policy scope.

## Common commands

```bash
.venv/bin/pytest -q                  # the whole offline suite
.venv/bin/ruff check . && .venv/bin/ruff format .
.venv/bin/lab config print           # effective config, secrets redacted
.venv/bin/lab demo                   # publish a corpus, ask, open the citation
.venv/bin/lab task queue --workspace <slug>     # durable queue state
.venv/bin/lab db verify-restore --workspace <slug>   # post-restore integrity
cd apps/web && npm test -- --run && npm run type-check
python tools/docs-check/docs_check.py
```

## Editing rules

- **Fakes raise on a miss.** A fixture-keyed fake that answered an unmatched
  request with a default would make the suite pass while proving nothing. Never
  add a permissive default to `adapters/connectors/fake.py` or
  `adapters/models/fake.py`.
- **No `datetime.now()` in domain or application code.** Time and ids are injected.
- **New state means a new named state**, not an overloaded `status` string.
- **New taxonomy member** goes in `contracts/errors.py` with a retry policy, and the
  parametrised test that walks the enum will demand one.
- **A disabled feature's dependencies must be exercised by the enabled path**, or
  the flag must remove the dependency. A staged control that rots is not staged.
- **Write the test that would have caught the bug**, at the layer where the bug
  lives.

## Where the interesting reasoning lives

- `domain/normalisation.py` — why "exact bytes" is not the gate, and what is
- `domain/identity.py` — the merge invariants, with the refusal reasons
- `application/extraction.py` — the five ordered steps and why the order matters
- `application/indexing.py` — why a release build is incremental by construction
- `provenance/fingerprints.py` — every input that invalidates a cache entry
- `adapters/connectors/fake.py` — the rule that makes the test suite meaningful

## Tool-specific guidance

`CLAUDE.md` is a pointer to this file. Do not duplicate architecture into both; CI
checks that tool guidance links to the same authority.
