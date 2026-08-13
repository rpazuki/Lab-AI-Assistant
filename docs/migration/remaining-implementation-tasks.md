Status: active
Owner: technical owner
Last verified: 2026-08-13
Source of truth: the code and tests each row names

# Audit findings and their resolution

A migration audit on 2026-08-13 compared the plan in `RLALab-AI-Assistant/migration`
against this repository and raised fifteen findings: four cutover blockers, six
production-readiness items, five hardening and documentation items.

A follow-up verification then found that the first pass had **overclaimed**: MIT-010
was complete for discovery and absent for acquisition, a rate-limited task was left
unclaimable rather than retried, and this page asserted a file count that was already
wrong. Those are recorded in
[Follow-up implementation issues](follow-up-implementation-issues.md) and are now
resolved too.

Each row below names what changed and the test that fails if it regresses. Where a
finding turned out to be a judgement call rather than a defect, the decision is
recorded in an ADR rather than in this page — a resolution log should not become a
second source of truth for a decision.

**A note on how the first version of this page failed.** It said "long waits defer the
task with its cursor intact" when only the cursor part was true, and it counted files
once and wrote the number down. Both are the same mistake: describing intent as
outcome. The rows below now point at tests that execute the path end to end, and the
verification block below asserts *properties* rather than a count that goes stale on
the next commit.

Two findings were more serious than they read, and are worth knowing about:

- **MIT-004** was a leak measured in what is *missing*: restricted claims filled the
  top-k window and pushed visible claims out of a caller's results.
- **MIT-006** silently attached a second acquisition to the first work that happened
  to share its bytes, discarding the second's rights decision — so a citation could
  point at the wrong paper.

## P0 — cutover blockers

| # | Finding | Resolution | Regression test |
|---|---|---|---|
| MIT-001 | The container built an empty connector registry, so every real discovery run would raise `UnknownConnector` | `live_connector_registry(settings)` builds the live set for the API, the workers, and the CLI from one place; fakes are passed in explicitly and never registered there; `lab config connectors` reports what a deployment has, with credentials as a boolean | `tests/contract/test_production_container.py` |
| MIT-002 | `submit()` accepted `actor_id` and discarded it; execution rebuilt it from `inputs` and otherwise fell back to the workspace uuid | `tasks.actor_id` is durable; the command carries the stored actor; a task with no actor anywhere **fails loudly** rather than being attributed to the workspace. The fingerprint still excludes the actor, and [the reason is in the docstring](../../packages/python/lab_assistant/provenance/fingerprints.py) — two stewards asking for the same run want one run | `tests/integration/test_task_actor_identity.py` |
| MIT-003 | Fixed limits of 500, 1,000, and 5,000 works capped acquisition planning, release drafting, retrieval visibility, and the conflict queue — below the predecessor's corpus size | Keyset pagination on `list_works` and `list_claims`, plus `application/pagination.py`; every lifecycle path iterates. Retrieval visibility also became one query per page instead of two per work, via `list_work_visibility` | `tests/integration/test_corpus_scale.py` (13,200 works) |
| MIT-004 | `claim_search` returned the top-k over every claim and `RetrievalService` filtered afterwards | `work_ids` is applied inside the candidate SQL for claims, as it already was for chunks; the post-ranking filter stays as defence in depth | `tests/policy/test_claim_and_count_filtering.py`, `tests/integration/test_sql_adapter.py` |

## P1 — production readiness

| # | Finding | Resolution | Regression test |
|---|---|---|---|
| MIT-005 | Chunk counts covered the whole release, so an aggregate revealed hidden volume | `count_chunks` takes the same allowlist; `withheld` is reported only to callers who may manage the release, and every caller learns their view is filtered. The decision is recorded in [data inventory](../security/data-inventory.md) | `tests/policy/test_claim_and_count_filtering.py`, `tests/e2e/test_acceptance_scenario.py` |
| MIT-006 | `register_asset` deduplicated on (workspace, digest) and returned the first row, losing the second acquisition's work, manifestation, and rights decision | Asset rows are unique per `(workspace, digest, work, manifestation)`. Bytes still deduplicate in the object store; acquisitions do not | `tests/integration/test_asset_provenance.py` |
| MIT-007 | The vector path joined embeddings to chunks on `chunk_key` alone, so copied chunks matched across releases | The join is on `(workspace_id, release_id, chunk_key)` in both the pgvector and the fallback path | `tests/integration/test_sql_adapter.py` |
| MIT-008 | `object_store_kind = s3` was a setting with no implementation; the container always built the filesystem store | `S3ObjectStore` over `httpx` with standard-library SigV4, wired by the setting, refusing to construct when unconfigured; readiness pings rather than writing probe objects. [ADR-0017](../architecture/adr/0017-object-storage-adapter.md) | `tests/contract/test_production_container.py` |
| MIT-009 | Scanning was required by default and the container set `malware_scanner=None`, so an upload enqueued a parse task that could never run | A real scanner is wired whenever either scan gate is on, and parsing is **not** queued for an asset that did not pass — the upload reports itself blocked, with the reason | `tests/contract/test_production_container.py`, `services/api/tests/test_api.py` |
| MIT-010 | `RateBudget` and `rate_policy` existed; live discovery called `connector.discover()` directly | **Every** source operation is budgeted: `discover` through `throttle`, `acquire` through `request`, with an AST test forbidding a direct connector call from the application layer. A rate-limited stage now returns `FAILED` with `rate_limited` rather than `BLOCKED`, so `Task.fail()` sets `not_before` and the task is claimable again — it resumes from the stored cursor instead of sitting unclaimable. Acquisition's terminal outcome maps back onto the retry taxonomy, so a rate limit is no longer reported as `invalid_content` and made permanent. [ADR-0018](../architecture/adr/0018-connector-rate-budgets.md) | `tests/integration/test_rate_limit.py`, `tests/contract/test_module_dependencies.py` |

## P2 — hardening and documentation alignment

| # | Finding | Resolution | Regression test |
|---|---|---|---|
| MIT-011 | `/api/v1/config` was public while the README promised authentication on every route | The route requires an authenticated member with the operations capability. Redaction is not a reason to publish topology | `services/api/tests/test_api.py` |
| MIT-012 | Production provider and model naming was implicit, and the predecessor documented a model string that does not exist | [ADR-0019](../architecture/adr/0019-model-provider-naming.md): provider `anthropic`, model `claude-sonnet-4-6`, recorded per answer and per extraction; `fake` is local-and-test only and is refused in production | `tests/contract/test_production_container.py` (production blockers) |
| MIT-013 | The API breaks from the predecessor with no written migration path | [ADR-0020](../architecture/adr/0020-predecessor-api-compatibility.md) plus the [API compatibility map](api-compatibility.md): every predecessor endpoint is mapped, retired with a reason, or covered by dual running | docs check (the map is a linked, status-headed page) |
| MIT-014 | `RawResponse.body` claimed to be exact upstream bytes and was sometimes a system-built envelope | `body_kind` says which, and an envelope's source bytes are stored as their own object with their own digest | `tests/contract/test_raw_response_provenance.py` |
| MIT-015 | Artifact hygiene could not be verified because the destination was not a git checkout | The repository is initialised. **Three `.env.*` files are tracked on purpose** — `.env.example`, `.env.compose`, `.env.server` — because they are templates and address files that hold no secrets (ADR-0012). Runtime secret files are not tracked and cannot be: `.env`, `.env.local`, private keys, `*Pass`, `*password*`, `*credentials*`, caches, `.venv`, `node_modules`, and `var/` (with `var/*` so the placeholder is actually tracked). `.gitignore` matches credential *shapes* rather than the single name that caused the predecessor's incident | the commands in the verification block below |

## Verification

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/ruff check .
```

```bash
python3 tools/docs-check/docs_check.py
```

```bash
.venv/bin/python tools/cli/run_benchmarks.py
```

```bash
cd apps/web && npm test -- --run && npm run type-check
```

Artifact hygiene, asserted as properties rather than as a file count that goes stale:

```bash
git ls-files | grep -E '^\.env'
```

Expected result: exactly `.env.compose`, `.env.example`, `.env.server` — the tracked
templates. A tracked `.env` or `.env.local` is a failure.

```bash
git ls-files | grep -E 'node_modules|\.venv|__pycache__|pytest_cache|ruff_cache|DS_Store|\.pem$|\.key$'
```

Expected result: no output.

At the time of writing: 716 backend tests, 21 frontend tests, three benchmark suites,
Ruff and the documentation checks clean, and `lab demo` exiting zero with a citation
that resolves.

## What this page is not

It is not a status board for the remaining milestones. M2 and M3 scope lives in
[the migration overview](README.md) and [ADR-0015](../architecture/adr/0015-delivery-profile.md).
