Status: active
Owner: technical owner
Last verified: 2026-08-13
Source of truth: the code and tests each section names

# Follow-up implementation issues

The second issue file for the migration audit. It recorded three problems found after
the resolution log claimed all fifteen findings were implemented — the headline being
that **"all fifteen are resolved" was not fully true**: MIT-010 was complete for
discovery and absent for acquisition, and a rate-limited task was left unclaimable
rather than retried.

**All three are now resolved.** Each section keeps the original finding, because the
way the first pass overclaimed is more instructive than the fix: it described intent
("long waits defer the task with its cursor intact") as outcome, when only the cursor
half was true. A test that executes the path through the orchestrator would have
caught it, and that test now exists.

## Issue 1 — MIT-010 was only partially implemented · resolved

### What was wrong

- Automatic acquisition called `connector.acquire(...)` directly, so acquisition
  requests spent no budget. Discovery respected a limit the source did not experience.
- A long rate-limit wait came back as `StageStatus.BLOCKED`. The orchestrator stores
  that verbatim: no `not_before`, and a blocked task is not claimable. The reproduction
  showed `task_state=blocked`, `not_before=None`, `retry_after=101` — cursor preserved,
  work stalled until an operator noticed.

### What changed

| Change | Where |
|---|---|
| `RateLimiter.request(...)` — the single-call sibling of `throttle` — wraps every automatic acquisition attempt | `application/rate_limit.py`, `application/acquisition.py` |
| A retryable discovery outcome returns `FAILED` with its error class instead of `BLOCKED`, so `Task.fail()` reads the retry table and sets `not_before` from the stored `Retry-After` | `application/discovery.py` |
| Acquisition's terminal outcome maps back onto the retry taxonomy. It previously reported a rate-limited attempt as `invalid_content`, which is **permanent** — the task failed for good over a limit that clears itself | `application/acquisition.py` (`_error_class_for_outcome`) |
| An AST test forbids awaiting or iterating `connector.discover/acquire/resolve/delta` from the application layer. `resolve` and change feeds have no call site yet; when they gain one it must go through the limiter, and now it cannot not | `tests/contract/test_module_dependencies.py` |

The guard has a self-test, because a guard nobody has watched fail might be matching
nothing.

### Tests

- `test_a_rate_limited_discovery_task_becomes_ready_with_a_wait` — executes a
  discovery task through the orchestrator against a budget holding a stored
  `Retry-After`, and asserts `TaskState.READY`, `not_before` in the future, and
  `error_class == rate_limited`.
- `test_the_retry_resumes_from_the_stored_cursor` — a two-page run with a one-token
  budget: page one is fetched and stored, page two defers, and the cursor is `page-2`.
  The retry resumes rather than re-fetching page one.
- `test_automatic_acquisition_spends_the_connector_budget` — a real acquisition attempt
  leaves a spent budget row with `in_flight == 0`.
- `test_the_bypass_guard_actually_catches_a_bypass` — two offending call shapes are
  detected; the two budgeted shapes are not.

## Issue 2 — the resolution log overstated artifact hygiene · resolved

### What was wrong

The log said "304 files, no `.env`, no caches". The follow-up found 317 tracked files
and three tracked `.env.*` files. Those files are intentional templates, so the defect
was the claim, not the repository.

### What changed

The MIT-015 row now says which `.env.*` files are tracked **and why** (templates and
address files, ADR-0012), and lists what may never be tracked. The verification block
asserts properties instead of a count that goes stale on the next commit:

```bash
git ls-files | grep -E '^\.env'
```

Expected: exactly `.env.compose`, `.env.example`, `.env.server`.

```bash
git ls-files | grep -E 'node_modules|\.venv|__pycache__|pytest_cache|ruff_cache|DS_Store|\.pem$|\.key$'
```

Expected: no output. The trailing `EOF` marker is gone and stays gone.

## Issue 3 — ADR-0016 claimed login rate limiting that did not exist · resolved

### What was wrong

ADR-0016 said login and invitation operations were rate-limited. Logins were audited;
nothing was throttled. A password endpoint with no limit is an offline-strength hash
defending an online-speed attack — and because argon2id is deliberately expensive, an
unthrottled login is also a denial-of-service surface against the API itself.

### What changed

`AccountService.authenticate` and `accept_invitation` acquire from **store-backed**
token buckets before verifying anything:

- **two buckets** — one per account, one per client origin. The per-origin bucket is
  what makes spraying many accounts from one place expensive; the per-account bucket is
  what protects one account from many places.
- **store-backed, not in-process.** A dictionary resets on deploy and is not shared
  between workers, which makes it a speed bump. This reuses the same row-locked bucket
  the connector budgets use (ADR-0018) — one mechanism, two very different callers.
- **keys are digests.** Rate limiting is not a reason to write plaintext emails or
  client addresses into a table operators read during incidents.
- **a successful sign-in clears the account bucket**, so two typos followed by the
  right password does not lock someone out of their own account.
- **`429 rate_limited`** with a `retry_after_seconds`, and still no hint about whether
  the account exists or which half was wrong.

`LAB_LOGIN_RATE_LIMIT_PER_MINUTE` (default 10) sets the rate, and ADR-0016 now
describes what the code does.

### Tests

`tests/integration/test_login_rate_limit.py` covers: repeated failures reaching the
throttling error; an unknown address throttling identically to a known one; the bucket
keyed by digest; a successful sign-in clearing the bucket; one origin unable to spray
several accounts; a different origin unaffected; and failures still audited without
plaintext. `services/api/tests/test_api.py::TestLoginThrottling` drives it through the
route and asserts the `429` and its error class.

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

716 backend tests, 21 frontend tests, three benchmark suites, Ruff and documentation
checks clean, and `lab demo` exiting zero with a citation that resolves.
