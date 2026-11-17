Status: active
Owner: technical owner
Last verified: 2026-11-17
Source of truth: inspection and local probes of repository commit `927c5ed`

# Shared archive: current-code audit

This is the evidence supporting the [architecture proposal](shared-archive-architecture.md)
and [implementation handoff](shared-archive-implementation-plan.md). Findings describe
this checkout, not a verified inventory of the remote VM. No cloud bucket or production
server was contacted. No application implementation was changed during this investigation.

## 1. Baseline and prior work

- Repo: `Lab-AI-Assistant`; inspected HEAD `927c5ed` (initial working tree clean).
- `927c5ed` — **Support persistent local storage for single-VM production deployments**.
- `8ea4adb` — shared-server deployment fixes and Red Hat installation documentation.
- [ADR-0017](../architecture/adr/0017-object-storage-adapter.md) introduced the S3 adapter.
- [ADR-0022](../architecture/adr/0022-single-vm-filesystem-storage.md) permitted production
  filesystem storage with explicit opt-in. S3 was retained, not removed.

This confirms that the earlier session did implement a local-storage alternative. It does
not establish which revision/configuration the current server is running.

## 2. Useful foundations

| Existing component | Evidence | Useful property |
|---|---|---|
| Content-addressed storage | [filesystem adapter](../../packages/python/lab_assistant/adapters/object_store/filesystem.py), `_key_for`, `_object_id` | SHA-256 keys; deterministic per-workspace object IDs; digest checking |
| Storage seam | [ports](../../packages/python/lab_assistant/application/ports.py), `ObjectStore` | Byte operations already separated from lifecycle services |
| Configured remote adapter | [composition root](../../services/api/src/lab_assistant_api/deps.py), `build_container`; [S3](../../packages/python/lab_assistant/adapters/object_store/s3.py) | `filesystem` and `s3` are real selections |
| Domain/adapter boundaries | [AGENTS](../../AGENTS.md), [module rules](../architecture/modules-and-dependencies.md) | Archive contracts can be added without provider SDKs in domain code |
| Release model | [release contract](../../packages/python/lab_assistant/contracts/release.py), [release service](../../packages/python/lab_assistant/application/releases.py) | Deterministic manifests, revisions, gates, aliases, outbox events |
| Incremental indexes | [indexing](../../packages/python/lab_assistant/application/indexing.py) | Copy unchanged release projections rather than re-embedding everything |
| Reuse fingerprints | [fingerprints](../../packages/python/lab_assistant/provenance/fingerprints.py) | Structured inputs for extraction, projections and task identity |
| Provenance/policy | [contracts](../../packages/python/lab_assistant/contracts/policy.py), [retrieval](../../packages/python/lab_assistant/application/retrieval.py) | Distinct actions, workspace scopes, filters before and after ranking |
| Durable queue | [tasks](../../packages/python/lab_assistant/application/tasks.py), [SQL repositories](../../packages/python/lab_assistant/adapters/db/repositories.py) | DB-backed leases/budgets and recoverable task rows |
| Recovery tools | [CLI](../../packages/python/lab_assistant/cli/main.py), [backup docs](../operations/backup-and-restore.md) | Existing starting point for restore verification |

## 3. Where the actual state lives

The [SQL schema](../../packages/python/lab_assistant/adapters/db/tables.py) contains these
families, all of which must be consciously classified when designing export/import:

| Family | Tables / important relationships |
|---|---|
| Identity/access | `users`, `user_external_identities`, `workspaces`, `memberships`, `groups`, `group_memberships`, `invitations`, `resource_grants` |
| Config/scope | `scope_versions`, `config_versions` |
| Object registry | `objects`: UUID, workspace, SHA, length, media/class, storage key, tombstone |
| Sources | `discovery_runs`, `source_requests`, `source_observations`, `source_assertions`, `source_cursors` |
| Identity graph | `works`, `work_identifiers`, `work_relations`, `identity_decisions` |
| Acquisition | `manifestations`, `rights_decisions`, `acquisition_plans`, `acquisition_attempts`, `assets` |
| Parsing/evidence | `parse_runs`, `renditions`, `evidence_spans` |
| Scientific graph | `entities`, `experiments`, `claims`, `claim_revisions`, `reviews` |
| Publication/search | `corpus_releases`, `release_items`, `search_aliases`, `search_chunks`, `claim_projections`, `chunk_embeddings_<dimensions>` |
| Operations | `tasks`, `rate_budgets`, `audit_events`, `outbox_events` |
| Researcher state | `chat_sessions`, `chat_messages`, `feedback` |
| Evaluation/migration | `evaluation_runs`, `legacy_object_map` |

Observed object writes in application code are raw/upstream source responses, canonical
records, acquired assets, rendition JSON, release manifests, validation reports and export
ZIPs. Claims/reviews/entities/evaluation results and search vectors are not automatically
exported into a complete reconstructible bucket catalog.

`ObjectStore` has no portable inventory/export/import method, no conditional-head operation,
no streaming interface, no provider version token and no read/write capability descriptor.
Both implementations accept/return whole `bytes` payloads.

## 4. Findings and implications

### F01 — Bucket-only reuse is not implemented (blocking for independent readers)

The current release manifest lists UUIDs, rendition digests, claim revision IDs and config
references. It does not carry full definitions of works, claims, reviews, rights, groups,
source relationships or embeddings. Another empty PostgreSQL database cannot resolve them.
The current object records do not describe a complete paper graph or list published releases.

**Change:** typed archive manifest + dependency-complete catalog + safe importer. Preserve
IDs and workspace identity. SQL remains necessary for querying even with remote objects.

### F02 — SQL object registration is missing from inspected write paths (**resolved**)

**Resolved 2026-11-17.** `objects` now has a repository on the unit of work
([`ObjectRegistryRepository`](../../packages/python/lab_assistant/application/ports.py)),
a domain record ([`ObjectRecord`](../../packages/python/lab_assistant/domain/objects.py))
and a registration helper
([`register_object`](../../packages/python/lab_assistant/application/objects.py)) called in
the same transaction as every dependent row: acquisition, parsing, discovery
(response, replay envelope and canonical record), release manifest, release
validation report and export package. SQLite now runs with `PRAGMA foreign_keys=ON`
([engine](../../packages/python/lab_assistant/adapters/db/engine.py)), so the offline
suite enforces what PostgreSQL enforces. Registration is insert-if-absent, so repeating
an ordinary write cannot clear a tombstone.

Two tests hold it: an AST guard that fails when a new `object_store.put` appears without
a registration in the same function
([`tests/contract/test_object_registration.py`](../../tests/contract/test_object_registration.py)),
and a lifecycle run over a real database that walks every schema foreign key into
`objects` and asserts it resolves
([`tests/integration/test_object_registry.py`](../../tests/integration/test_object_registry.py)).
Enabling the constraints also exposed a second fabricated reference: the demo corpus seed
invented a `source_observation_id`, and now writes a real scope, discovery run, request and
observation behind each title assertion.

Verified on PostgreSQL 16.13 with pgvector 0.8.2: the lifecycle passes both against a
schema built from the application's `MetaData` and against one built by `alembic upgrade
head`, which is what a deployment runs. `psql \\d objects` confirms the twelve foreign
keys (the audit said thirteen; twelve is the measured number).

Still open: backfilling historical references in a deployed database, which have no
`objects` rows either, and reconciling orphan payloads. That needs the real inventory.

The original finding follows.


`assets.object_id`, `renditions.object_id` and other references are foreign keys to
`objects.id`. Object adapters write only their own metadata records, while acquisition and
parsing then call `uow.assets.add_asset` / `uow.renditions.add_rendition`. Those repository
methods only upsert the domain row. No insertion into `t.objects` or equivalent registry
repository was found in these paths. The composition root does not wrap the object store
with database registration.

The ordinary SQLite SQL test suite does not enable `PRAGMA foreign_keys=ON`. A probe
turning it on reproduces a failure at `add_asset`: the asset's `object_id` is missing from
`objects`. This is not a live PostgreSQL test, but its schema constraint and the missing
write path are concrete evidence of an integrity gap.

**Change:** explicit object-registration repository and transaction rules for every caller;
backfill verified old references; orphan reconciliation. Run the full lifecycle on real
PostgreSQL and enable foreign keys in SQLite tests. Do not remove constraints to pass tests.

### F03 — S3 missing and forbidden are conflated (**resolved**)

**Resolved 2026-11-17 by [ADR-0023](../architecture/adr/0023-object-storage-sdk.md).**
The hand-written signer is gone; `S3ObjectStore` now runs on `boto3` behind one bounded
thread boundary. Missing, denied, restore-pending, conflict, throttled, unreachable, timed
out, unsupported and corrupt are nine distinct outcomes instead of one `None`.
`ErrorClass.STORAGE_DENIED` and `ErrorClass.STORAGE_RESTORE_PENDING` joined the taxonomy
with their retry policies. Content is created with `If-None-Match: *` and a record update
carries `If-Match` on the version it read, so a stale writer cannot undo a tombstone. The
credential chain replaces static-keys-only, and an optional prefix isolates a shared bucket
without ever being stored on a record.

Writing the translation exposed one more conflation of the same shape: a cold object
answers `InvalidObjectState` with HTTP **403**, so reading the status before the error code
reported "being restored" as "not allowed". The code is now read first, and
`tests/contract/test_s3_object_store.py` holds it — 26 tests against `botocore.stub.Stubber`,
which is the coverage ADR-0017 said it had.

**Not verified:** no request has been made to a live provider. Presigned-URL acceptance,
credential refresh mid-job, conditional-write support on a specific gateway and cold-class
behaviour all need the opt-in live suite. Streaming bodies, multipart upload and a native
GCS adapter remain phase 1 work.

The original findings follow.


`S3ObjectStore._request(..., allow_missing=True)` returns `None` for both 403 and 404.
`head` treats this as absent; `put` can consequently attempt a write. A reader whose IAM
permissions are wrong must not behave as if data does not exist and needs reacquisition.

**Change:** separate missing/denied/transient/cold/unavailable/corrupt outcomes, map them to
the error taxonomy, and never fall back to writes or source/model calls on an auth failure.

### F04 — Handwritten S3 signing needs repair or replacement (**resolved**, see F03)

- `_presign` computes a signature and then adds `x-lab-expires-at` to the query. That
  additional query parameter was not in the signed canonical query.
- `_sign` and `_presign` use `httpx.URL(endpoint).host`, which omits the port for a custom
  endpoint such as `https://store.invalid:9443`; the request URL includes that port.
- The canonical URI does not account for an endpoint path prefix.
- `S3StoreConfig` accepts only static access/secret keys: no session token, workload identity,
  refreshing credential provider or native GCS adapter.
- `path_style=False` relies on endpoint construction outside this config and is not
  exposed by `from_settings`; supported endpoint forms need an explicit contract.

**Change:** favour maintained SDK adapters and a superseding ADR; pin/test provider versions.
Do not advertise current presigned URLs as live-provider-verified. Short-lived role
credentials and signing behaviour need real-provider tests, not only construction checks.

### F05 — Writes are check-then-put, not conditional (**partly resolved**, see F03)

`put` reads a metadata record, HEADs the content, then issues ordinary PUTs. `_write_record`
also issues an unconditional PUT. Concurrent writers can race; an older record write can
resurrect metadata after a tombstone. A HEAD is not a lock. There is no authoritative
writer ID, epoch, atomic catalog pointer or cross-installation lease.

**Change:** conditional immutable writes and a compare-and-swap publication head. Mutable
policy/tombstones are versioned control records. Enforce one publisher and fence handover.

**Done:** conditional create and compare-and-swap on the object and record keys.
**Still open:** the publication head itself, writer identity and epoch, and the outbox
reconciliation — phase 2. The adapter now has the primitives those need.

### F06 — Namespace/isolation documentation is stronger than the adapter implementation

Content keys are global by digest; metadata UUIDs depend on workspace. The adapter does
not provide the per-workspace encryption claimed in ADR-0004. There is no library prefix
configuration. A broad bucket reader can read all bytes its cloud credentials allow,
regardless of the web application's user-policy checks.

**Change:** library isolation through prefixes/buckets/IAM and optional key policy; explicitly
retain app policy as a second layer. Plan migration of old keys, shared references and
current citations. Do not claim independent encryption or tenant isolation without tests.

### F07 — No installation-level read-only mode

[Settings](../../packages/python/lab_assistant/config.py) and the composition root select
storage/model providers but no reader/publisher capability. Every container builds
connectors, parsers, extractor, scanner and handler registry. Disabling workers alone still
leaves mutation routes/CLI available. A user role is not an installation role.

**Change:** capability checks at API, application services, CLI and task claims; role-aware
composition and least-privilege DB/cloud credentials. Reader should not require source
API keys or a malware scanner just to serve already approved content.

### F08 — Reader interactions require writes (confirmed from code)

[Accounts](../../packages/python/lab_assistant/application/accounts.py) writes last login,
rate buckets and audit. [Chat](../../packages/python/lab_assistant/application/chat.py)
writes sessions/messages/feedback/audit. Exports write an object and audit. The current
single `ServiceContext.uow_factory` spans both corpus and operational state.

**Change:** retain a writable local operational DB area, separate imported corpus grants,
and route export scratch output to private/local storage. Define permitted reader features
rather than blocking all POST methods (search is itself POST).

### F09 — Scientific exports are not restorable library packages

[ExportService](../../packages/python/lab_assistant/application/export.py) produces a
policy-filtered BagIt/RO-Crate-inspired reporting package, not full-fidelity replication.
The inspected implementation has an unpopulated entities branch, assets manifest rows
hardcoded as not included, no full rendition/vector recovery set, and no corresponding
archive importer. `include_assets` is not proof that payloads were included. The ZIP is
assembled in memory.

**Change:** new, explicitly versioned archival contract; reuse canonical/checksum utilities,
not the claim that the existing export format restores the application. Preserve reporting
exports as a separate purpose and correct their metadata before using them operationally.

### F10 — Expensive computation reuse depends on the DB

Extraction checks `find_revision_by_fingerprint`; parsing completion and task results are
also database state. Incremental indexing copies rows from another release in the same
search store. There is no archive-level completed-result index or portable vector bundle.
A source download's content hash is known only after acquisition, so byte deduplication
alone does not prevent repeated downloads from a new VM.

The fingerprint helper is a useful start, but its call sites need audit: extraction passes
`settings.llm_model` as model revision, the rendition schema version as parser version,
and node-text prefixes as `span_digests`. The complete rendition digest reduces some
collision risk, but labels are not complete actual tool provenance. A result may have no
proposals, in which case finding one claim revision is not a reliable completion ledger.

**Change:** archive a typed stage result (including empty/refused/truncated/failure outcomes),
actual versioned inputs, cost/request identity and valid reuse conditions. Keep transient
failures retryable. Look up authorised prior acquisitions/results before source/model calls.

### F11 — Claimed model-envelope archival is not wired in the inspected extraction path

Architecture/data-inventory docs say model request envelopes are stored as objects.
`ModelExtractor` calls the provider and returns proposals/usage; extraction stores claims,
revisions and audit, but has no `object_store.put` for the model request/response. Do not
assume every costly response can already be recovered or revalidated without another call.

**Change:** policy-controlled request/result persistence with explicit redaction/retention;
never store provider credentials or assume all prompts/outputs may be retained indefinitely.

### F12 — Restore verification covers only part of the graph (**resolved**)

**Resolved 2026-11-17.** `verify-restore` now keyset-pages every release instead of
reading a thousand, treats a manifest that will not parse as a finding instead of an
exception that hid every finding after it, and runs the sweep below over the whole graph.
Measured on the same seeded corpus that previously reported `objects_checked: 1` of four:
12 reference sources swept, 3 references checked, 4 payloads re-digested. Corrupting one
payload now produces two independent findings and exit 1, where it previously printed
`restore verified`.

New: [`StorageInventoryService`](../../packages/python/lab_assistant/application/storage_inventory.py),
behind `lab storage scan` and `lab storage backfill`. It walks both directions — every
column that points at `objects.id`, read from the schema rather than a hand-written list,
and every registered object's bytes — because a check that answered only one of those
would pass on a restore with an empty bucket. Findings are typed and separated into those
that block serving and those that do not; missing, denied, cold and corrupt stay distinct,
using the error classes [ADR-0023](../architecture/adr/0023-object-storage-sdk.md) added.

The round-trip defect this finding predicted was real: `canonical_bytes` writes a nested
`chunker` object while the model keeps `chunker_id`/`chunker_version` flat, so parsing a
stored manifest dropped both and substituted the defaults. Any release with a non-default
chunker re-digested to a different value, and the restore check reported a mismatch of its
own making. Fixed by accepting the canonical shape on the way in — not by changing
`canonical_bytes`, which every stored `manifest_sha256` was computed over.

**Correction to this document's earlier framing.** It said historical references would need
backfilling on a deployed database. On PostgreSQL they cannot exist: the foreign key refuses
the insert, verified here by `DELETE FROM objects` being rejected with "still referenced from
table assets". So a PostgreSQL deployment of this code has not been writing dangling rows —
it has been *failing* to write assets, renditions and source responses at all. Backfill
remains for a restore that did not recreate its constraints. What a deployed object store
may well hold is the other side: payloads uploaded by transactions that then failed, which
the sweep reports as orphans.

The original finding follows.


`cli/main.py::_verify_restore` lists at most 1,000 releases, checks servable release manifests
and their rendition digests plus aliases and audit. Measured on PostgreSQL against a seeded
demo corpus whose registry holds four objects, it reports `objects_checked: 1` and
`restore verified`: the asset bytes, the release manifest object and the validation report
are never looked at. The gap is now a number, not an inference. It does not prove all original assets,
claims, source raw objects, configs, vectors, private draft outputs or exports exist. It
cannot reconstruct missing DB rows from the bucket. A malformed manifest can escape the
intended aggregated finding flow. `ReleaseManifest.canonical_bytes` uses a nested `chunker`
shape while model fields are `chunker_id`/`chunker_version`: round-trip tests must cover
non-default chunkers before using this contract for portable manifests.

**Change:** complete keyset-paged reachability verification, strict portable round trips,
explicit required/optional/missing states, count reporting and compatible projection checks.
Keep restore check failures actionable rather than claiming blanket integrity.

### F13 — Existing backup scripts are topology-specific and filesystem-only (**resolved**)

[backup.sh](../../infra/backup/backup.sh) performs `pg_basebackup` then filesystem `rsync`;
its explanatory comment describes a different ordering. This is not a general consistent
checkpoint of database plus mutable metadata/tombstones, and does not back up S3.
[restore.sh](../../infra/backup/restore.sh) hardcodes `lab-assistant_pgdata` and
`lab-assistant_objects`, removes existing target database files and merges object files.
It assumes versions, ownership and WAL availability rather than establishing portability.
The [shared VM guide](../fresh_install_linux_redhat.md) contains a more appropriate
stopped-writer backup procedure, but the paths need consolidating.

**Change:** portable dump/checkpoint manifests, resolved Compose volume names, fresh restore
targets, provider-aware inventories, off-VM backup and tested key recovery. No destructive
restore into a guessed volume. Separate public library artifacts from private DB dumps.

**Resolved.** `lab backup create / verify / restore` with a committed recovery manifest;
payloads copied through the `ObjectStore` port, so a bucket is backed up like a directory;
a preflight that refuses a missing or older extension, an older PostgreSQL major and a
dump from a schema this code has never heard of; `infra/backup/lib/volumes.sh` resolving
Compose volume names from the host and refusing on no match, on ambiguity, and on a target
that is not empty. The restore's `rm -rf` of a guessed volume is gone. The private
recovery format (`contracts/backup.py`) is deliberately separate from the portable library
archive (`contracts/archive.py`): one carries password hashes and unreviewed evidence and
must never leave encrypted operator storage, the other is designed to be handed to a
reader. Key recovery and an actual off-VM drill remain untested — see the plan's ledger.

### F14 — Deployment rollback assumes code rollback fixes schema state

[scripts/deploy.sh](../../scripts/deploy.sh) runs migrations only for changed `database/`
paths and resets Git/rebuilds old services on later failure. It does not roll the database
back. A new archive/schema format needs an expand/contract compatibility policy, explicit
migration state and recovery points. The initial Alembic migration imports live `METADATA`,
so modifying today's tables changes what a historical fresh migration creates; future
migration work must address historical-schema stability rather than hide drift.

**Change:** test forward/old-reader compatibility, never reset to incompatible code after a
schema/archive commit, keep previous compatible image and snapshot activation available.
Migration history and the deployed-version record must not be inferred just from Git HEAD.

### F15 — Readiness is not a sufficient import/role check

`/ready` checks DB access and S3 zero-key LIST; filesystem readiness performs a PUT. It
returns a degraded JSON body without an HTTP 503. A tightly scoped reader may have GET
permission for known manifests but not bucket-wide listing. The deployment helper parses
`status`, but generic HTTP health tools may incorrectly treat degradation as ready.

**Change:** role-specific checks (known sentinel/manifest GET for reader, explicit publisher
preflight for writes), schema/active-projection/policy age checks, meaningful HTTP status,
and separate process liveness from serving readiness. No destructive probe writes.

### F16 — the PostgreSQL integration suite had never run (found while verifying F02)

`CONTRIBUTING.md` documents `LAB_TEST_DATABASE_URL=... pytest tests/integration`, and the
`postgres` marker exists. Running it produced fourteen failures in `test_sql_adapter.py`
alone, every one a duplicate key. SQLite's `:memory:` hands each engine a fresh database;
one PostgreSQL database is shared by the whole session, and `SequentialIds` then gives the
second test the same UUIDs as the first. The second test failed on a primary key before
reaching its subject. The `postgres_url` fixture in `tests/conftest.py` is referenced by no
test at all.

This is how F02 survived: the only database that enforces the constraint was the only
database the suite never ran against.

**Changed:** `reset_database(engine)` in
[testing.py](../../packages/python/lab_assistant/testing.py), called by both SQL fixtures
before each test. It empties only tables declared in `METADATA` — a helper pointed at an
operator-supplied URL has no business dropping a schema it did not create. With it,
`tests/integration/test_sql_adapter.py` passes on PostgreSQL for the first time, and the
full suite is 780 passed against PostgreSQL versus 761 offline.

**Still to do:** make the PostgreSQL run part of CI rather than an opt-in nobody exercised.

### F17 — `alembic check` fails against any migrated database (**resolved**)

**Resolved 2026-11-17.** The three expression and partial indexes are declared in
`tables.py` with `.ddl_if(dialect="postgresql")`, so autogenerate compares them instead of
proposing to drop them. `alembic check` now reports "No new upgrade operations detected"
against both a freshly migrated database and one migrated before the change, and SQLite is
untouched. `tests/contract/test_schema_declarations.py` fails if a later migration creates
an index in raw SQL that the metadata does not describe.

The original finding follows.


`env.py` states that CI runs `alembic check` as the drift guard. Against a database built by
`alembic upgrade head` it reports three removed indexes — `ix_search_chunks_fts`,
`ix_claim_projections_subject` and `ix_tasks_claimable` — because the migration creates them
with `op.execute` and `tables.py` does not declare them. Autogenerate therefore wants to drop
them on every run, so the documented drift guard cannot be passing against a real migrated
database.

**Change:** declare the expression indexes in the metadata (or exclude them explicitly in
`env.py`'s `include_object`) so the check means what it claims. Until then, a genuine future
drift is indistinguishable from this standing noise, which matters as soon as phase 2 starts
adding tables.

### F18 — documented guards that were never wired (pattern, not a single defect)

Three claims in this repository described enforcement that did not exist, each found by
trying to use it rather than by reading it:

| Claim | Where | Reality |
|---|---|---|
| "adapter contract tests against a stubbed transport" | ADR-0017 | no S3 test existed |
| "the drift check in CI runs `alembic check`" | `database/migrations/env.py` | failed against any migrated database (F17) |
| "CI regenerates them and fails on drift" | `tools/cli/generate_schemas.py` | CI checks `openapi.json` only |

Each is now true: `tests/contract/test_s3_object_store.py`, the declared expression
indexes, and `tests/contract/test_contract_schemas.py`. The pattern is worth naming
because the next one will read just as convincing. A sentence in a docstring is not a
control, and the cheapest way to find these is to run the thing the sentence describes.

## 5. Verification performed

### Ordinary targeted baseline

Ran, offline:

```bash
.venv/bin/pytest -q tests/contract/test_adapter_contracts.py \
  tests/contract/test_production_container.py \
  tests/integration/test_filesystem_shared_storage.py \
  tests/integration/test_operations_cli.py tests/integration/test_sql_adapter.py
```

Result: **95 tests passed**, exit 0. These exercise current local behaviour; they do not
prove live S3/GCS interoperability, multi-VM sharing, restored data completeness or IAM.

### Foreign-key probe

Without changing repository files, installed a SQLAlchemy connection listener for the
probe process and reran the SQL tests:

```python
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def enable_fk(dbapi, record):
    cursor = dbapi.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

raise SystemExit(pytest.main([
    "-q", "tests/integration/test_sql_adapter.py", "--tb=short", "--maxfail=2"
]))
```

Run this only in the SQLite offline test environment, without `LAB_TEST_DATABASE_URL`.
Result: **13 passed, 1 failed** at `TestEndToEndOverSql.test_release_publish_and_answer`,
`uow.assets.add_asset`, with `FOREIGN KEY constraint failed`; the referenced object record
was absent. A real PostgreSQL reproduction and full lifecycle repair are phase-0 work.

### Real PostgreSQL run, 2026-11-17

A disposable `pgvector/pgvector:pg16` container (PostgreSQL 16.13, pgvector 0.8.2) on port
55432, no shared volume, three scratch databases. Nothing touched an existing container,
volume or deployment.

```bash
docker run -d --rm --name lab-assistant-phase0-pg \
  -e POSTGRES_PASSWORD=... -e POSTGRES_DB=lab_assistant_test \
  -p 55432:5432 pgvector/pgvector:pg16
LAB_TEST_DATABASE_URL=postgresql+asyncpg://postgres:...@localhost:55432/lab_assistant_test \
  .venv/bin/pytest
```

| Check | Result |
|---|---|
| Full suite with PostgreSQL enabled | 780 passed (761 offline) |
| `tests/integration/test_sql_adapter.py` on PostgreSQL | 28 passed; 14 failed before F16 was fixed |
| Object-registry lifecycle on PostgreSQL | passed, every foreign key into `objects` resolves |
| `alembic upgrade head`, empty database | applied `0001_initial`; twelve foreign keys into `objects` present |
| `alembic upgrade head`, populated database | no-op at head, rows intact |
| Lifecycle against the Alembic-built schema | passed — production DDL, not only `create_all` |
| `alembic check` | **fails**, see F17 |
| `lab db verify-restore` on seeded PostgreSQL | `restore verified`, `objects_checked: 1` of 4 registered, see F12 |

### Backup and restore run, 2026-11-17

A second disposable `pgvector/pgvector:pg16` container (PostgreSQL 16.13, pgvector 0.8.2)
on port 55433, `pg_dump`/`pg_restore` 16.13 from Homebrew. Three scratch databases, all
dropped afterwards; the container was removed. Nothing touched an existing container,
volume or deployment.

| Check | Result |
|---|---|
| Full suite with PostgreSQL enabled | 985 passed, 1 skipped (live S3 only) |
| `tests/integration/test_backup_and_restore.py` | 48 passed, 5 of them against real PostgreSQL |
| `alembic upgrade head` then `alembic check` | applied, then **clean** — this change adds no schema |
| `lab backup create` against real PostgreSQL | 150 KB dump, 3 objects copied and digest-verified, manifest written |
| `lab backup verify` | 3 components, 3 objects, `verified: true` |
| `lab backup restore` into the **same** database | refused, exit 1: `destination_not_empty` (52 tables) and `clone_keeps_origin_identity` |
| `lab backup restore` into a fresh database without pgvector | refused, exit 1: `extension_missing` — the rule firing on a real missing extension |
| `lab backup restore --apply` into a fresh database with pgvector | applied; control plane and all 3 payloads restored; `may_publish: false` |
| `lab db verify-restore` on the restored installation | `problems: []`; advisory orphans only |

Two defects were found by running this rather than by reading it, and both are fixed:

1. **`pg_restore` into a populated database fails partway**, on `relation already exists`
   — and it fails *after* the restore has copied the payloads in. There was no rule that
   a logical restore needs an empty destination. There is now (`destination_not_empty`),
   and `--clean` is the deliberate alternative.
2. **The restore copied objects before replaying the dump.** The copy is driven by the
   *destination's* object registry, which does not exist until the dump creates it, so on
   a fresh database it raised `UndefinedTable`. `create` puts payloads first, for a good
   reason — a backup must never hold a control plane newer than its payloads — and the
   symmetry is false: restoring is not the mirror image of backing up. The offline suite
   could not catch this, because an in-memory registry has its tables whether or not a
   dump was replayed; `TestAgainstRealPostgres::test_a_restore_into_a_fresh_database`
   does, and fails when the two steps are swapped.

Still unproven: a restore onto a second machine, backup encryption and key recovery,
S3 as either end of a backup, and any measured RTO.

This does not prove S3/GCS interoperability, multi-VM sharing or IAM behaviour, and the
seeded corpus is synthetic. No production database or bucket was contacted.

### S3 structural probes (synthetic values, mock HTTP only)

Constructed `S3ObjectStore` with endpoint `https://store.invalid:9443` and an
`httpx.MockTransport` returning HTTP 403. Observed:

```text
403 record lookup returns: None
Signed Host: store.invalid
URL authority: store.invalid:9443
Post-signature extra query parameter present: True
```

This confirms the structural findings in F03/F04. It does not claim an actual cloud
request was executed. The checked-in tests select the S3 class, but no S3 transport/signature
contract coverage was found by searching the current tests for `S3`/`MockTransport`/
`presign`. ADR-0017's described stub-transport coverage is not present in this checkout.

## 6. What remains unknown

- Actual deployed image/revision, current DB constraints/migrations, volumes and corpus size.
- Real institutional bucket/IAM/KMS availability, region, network routes and provider terms.
- Existing paper entitlements and which artifacts may be replicated to reader installations.
- Actual parser/model versions used to create historical records when not recorded fully.
- Time/cost of SQL import/index build and remote citation fetch at real scale.
- Live cloud signer/streaming/identity lifecycle. Real PostgreSQL migration behaviour is
  no longer unknown for the current single revision: see the run recorded in section 5.
  Behaviour of a *second* revision against a populated database remains untested.

The implementation must measure these and retain explicit unknowns in migrated provenance.
Never manufacture a historical parser version or licence decision to make import pass.
