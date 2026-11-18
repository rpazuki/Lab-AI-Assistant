Status: proposed
Owner: technical owner + operator + data steward
Last verified: 2026-11-17
Source of truth: implementation handoff for design based on commit `927c5ed`; phase 0 in progress

# Shared archive implementation handoff

Start with the [architecture proposal](shared-archive-architecture.md) and
[code audit](shared-archive-code-audit.md). This file is a staged work plan, not an
instruction to deploy an unimplemented feature. Proposed command names, settings,
tables and files below do not exist yet unless explicitly described as existing.

## 1. Instructions for the next implementation session

1. Work in `Lab-AI-Assistant`, currently located at
   `/Users/roozbeh/Research/Bezos_Centre/codes/Lab-AI-Assistant`. Read `AGENTS.md` and its
   required architecture/contribution links. Do not edit the other two projects.
2. Check Git status/HEAD and compare to audited `927c5ed`; recheck findings on changed files.
   Preserve unrelated edits. Current proposal is documentation only.
3. Implement phase 0, then phase 1. Do not start with a UI role dropdown or bucket config
   toggle and declare multi-instance support complete.
4. Use the existing module boundaries: typed contracts, domain rules, application ports,
   provider/SQL adapters and thin API/CLI. Add new superseding ADRs when changing accepted
   decisions; leave existing accepted decision text intact.
5. Establish real PostgreSQL and opt-in provider acceptance environments with synthetic
   data. Never point tests at the production database/bucket or real licensed papers.
6. Before production storage migration, obtain the chosen backend/region/IAM and rights
   decisions below. Planning and local implementation can proceed without those credentials.
7. Maintain a completion ledger against the phases and tests in this document. Report what
   is actually validated; ordinary offline tests do not prove S3/GCS interoperability.

**Definition of success:** a fresh authorised reader, starting with no corpus DB and
GET-only archive credentials, imports an approved snapshot and can search, ask and open
citations without downloading from scholarly sources, reparsing papers, re-extracting
claims or regenerating stored embeddings. A fenced handover restores a publisher that can
continue curation without silently losing unpublished work. A single-instance remote-store
move is a separately useful and earlier acceptance milestone.

## 2. Proposed contracts and interfaces

### Storage and archive ports

Retain compatibility with existing `ObjectStore` during migration, but introduce explicit
capabilities instead of overloading a byte store with the entire library protocol:

| Port / contract | Responsibilities |
|---|---|
| `BlobStore` | Streamed immutable reads/writes, SHA/size checks, storage-version metadata, provider error translation |
| `ArchiveControlStore` | Read descriptor/head; create-only and compare-and-swap updates; opaque ETag/generation tokens |
| `ObjectRegistryRepository` | Transactional SQL metadata registration and reference lookup; no provider calls |
| `ArchiveCatalogRepository` | Library/package/snapshot/import metadata and typed dependency enumeration |
| `StageResultArchive` | Durable completed stage envelopes, reuse fingerprints and policy-scoped lookup |
| `InstanceCapabilities` | Allowed library/operational actions, role and installation identity |
| `CorpusReadUnitOfWork` | Read imported/published graph with policy scope and generation pinned |
| `OperationalUnitOfWork` | Accounts, memberships, chat, audit, login budgets and local state |
| `PublisherUnitOfWork` | Working graph mutations/tasks and publication outbox; publisher only |
| `ArtifactCache` | Bounded verified local copies; never authority for current rights |

These names are design guidance, not a mandate for nine new abstractions immediately.
Keep closely related interfaces small; do not move application transactions into adapters.
Adapters must be testable without real cloud SDK credentials or the wall clock.

Separate conditional-create and compare-and-swap from ordinary content reads. In-memory
fakes must implement the same failure semantics (including precondition failures), not
quietly permit overwrite. Filesystem support remains for single-VM operation; its POSIX
lock is not a multi-host writer protocol.

### Archive schema v1

Required snapshot fields:

```text
format_name + format_version
minimum_reader_protocol + required_features
library_id + source_workspace_id + snapshot_id
origin_installation_id + writer_epoch + publication_sequence
parent_snapshot_id + created_at + producer_app_version
scientific_release_ids + selected_alias_targets
policy_sequence + policy_artifact_digest
catalog_shards[{kind, schema_version, sha256, byte_length, row_count, location}]
package_refs[{work_id, package_revision, manifest_digest}]
projection_refs[{release_id, chunker_digest, embedding_profile_digest, shards}]
required_object_inventory_digest + missing_optional_artifacts
provenance_checkpoint + signature/key_identifier
```

Use stable canonicalisation and known Unicode/number/date rules. Do not hash provider URLs
or signatures into logical content identity. Signature covers the manifest digest and
relevant identity/sequence fields using a versioned envelope. Pin publisher verification
keys through a trusted descriptor/configuration; key rotation needs an authenticated
transition. Cloud bucket administration alone must not be treated as proof of who approved
scientific results. Hashes detect corruption; signatures authenticate an approved producer.

A shard contains versioned typed records, not SQL statements. References carry source
namespace/library and ID. Distinguish missing-required from withheld/optional/expired.
Store supported unknown fields for forward-compatible minor versions where safe; refuse
unknown required semantics and major versions. Set explicit import resource limits.

Stage result envelope must include input artifact digests, full processing recipe,
policy/workspace scope, actual tool/model versions, completion outcome and completeness,
output artifact list, provider request identity where available, token/cost metrics,
start/end timestamps, originating actor attribution and reuse restrictions. A successful
zero-proposal result is still a result; a retryable timeout is not a permanent cache hit.

### Configuration and compatibility

Keep `LAB_ENV` as the existing address/deployment scenario. Add an independent validated
installation-role setting, stable library/installation identity, archive binding, policy
freshness limit, cache quota, importer resource limits and supported format range. Exact
environment-variable names are implementation decisions. Extend current backend selection
to native GCS without silently interpreting an unknown value as filesystem storage.

Provider endpoints/buckets belong to the address configuration governed by ADR-0012;
credentials come from approved environment/identity providers. Workspace configuration must
not be allowed to elevate a VM's publishing role or change its secret/credential identity.
Expose effective role/capabilities and redacted config through the existing operator view.
Fail startup for an invalid combination (reader with workers enabled, unknown archive
format, incompatible local schema), with a recovery mode for diagnosis.

Keep a version compatibility matrix: producer version, archive major/minor version,
minimum reader protocol, DB schema version and embedding/chunker profile. Upgrade readers
to understand an additive format before producers make its features mandatory. Never
require every VM to upgrade in exactly the same moment. Maintain fixtures from previous
supported formats and an explicit deprecation window.

### Database changes (additive first)

Likely new tables/columns, final names subject to review:

- `libraries`, `installations`, `library_bindings` (source workspace/group mapping).
- `archive_publications` / publication outbox (prepared, verified, committed, acknowledged,
  failed; snapshot digest, expected head version, epoch and recovery evidence).
- `archive_artifacts` / object registry additions (backend namespace, logical digest,
  provider version token, verification status; distinguish v1 legacy records).
- `stage_results` (fingerprint, status, complete output manifest, reuse policy).
- `archive_imports`, `import_shards`, `import_generations`, `active_library_generation`.
- `policy_checkpoints` and revocation projection with freshness/expiry.
- Origin actor/reference tables, separate from authentication `users`.
- Per-installation audit identity/checkpoint metadata; no splicing foreign events into
  a local hash chain.

Reader immutable generations must not collide with current PKs. Existing `works` is keyed
by ID, and `search_chunks`/vectors by release/chunk/profile: importing changed metadata
in place can violate snapshot consistency even when the alias switches atomically. Choose
one tested strategy before schema implementation:

1. Generation-keyed corpus tables/views (recommended long-term); every query pins generation.
2. Separate staging/active corpus schemas with a connection-level generation binding.

Do not rename tables under running transactions without a tested connection/query plan.
Retain current publisher tables initially; define versioned immutable read records for
reader imports. Include a migration bridge and old-chat citation lookup by release identity.

SQL roles: migrator/owner, publisher app/worker, reader importer, reader API, backup,
retention operator. Reader API can write only its operational tables. Importer cannot alter
accounts or promote its cloud role. Avoid deployment DB superuser credentials in web services.

## 3. Phased delivery with acceptance gates

### Phase 0 — Correctness baseline and archive design fixtures (first; medium)

Work:

- Reproduce and fix F02 registration with real PostgreSQL constraints; explicitly register
  every stored object before dependent rows in the same graph transaction. Verified object
  upload happens before SQL commit; rollback leaves a tracked/reconcilable orphan.
- Audit all object write callers: discovery raw/upstream/canonical, acquisition, parsing,
  release/validation, exports, new model envelopes. Verify references are retained even
  when no existing domain field captures the validation report ID.
- Enable SQLite foreign keys and repair fixtures/order/circular relations rather than
  weakening production constraints. Exercise migrations on empty and populated PG databases.
- Fix missing/forbidden handling and signer defects, or replace adapter in phase 1 before
  any cloud migration. Add failure-injection tests.
- Strengthen `verify-restore` with full graph inventory/pagination and strict manifest round
  trips, including non-default chunker values. Treat malformed manifests as findings.
- Document historical provenance that cannot be recovered; do not synthesize it.
- Produce an archive fixture containing two releases, a revised paper, restricted evidence,
  review history, an empty extraction result and compatible vectors.

Acceptance: ordinary suite + foreign-key-enabled SQL lifecycle + real PG lifecycle pass;
restore check catches missing original bytes, missing rendition, missing claims/config,
missing projection and >1,000-release tail corruption. No app functionality removed.

Deliverable: reliability PR(s) and a golden portable fixture schema draft. This phase
improves existing deployments even if independent readers are deferred.

### Phase 1 — Reliable remote storage and a portable single installation (medium/large)

Work:

- Implement maintained S3 adapter and native GCS adapter (or choose S3 first, GCS next,
  while documenting that only tested providers are supported). Amend ADR-0017 through a
  new proposed/accepted decision, not an undocumented replacement.
  **S3 done ([ADR-0023](../architecture/adr/0023-object-storage-sdk.md)); GCS outstanding.**
- Stream and checksum payloads; support standard credential chains, temporary session
  credentials/refresh, configurable permitted endpoints, TLS and region.
  **Done:** credential chain, checksums verified by the provider, `put_stream` /
  `stream` across all three stores with one shared spooling path, multipart above a
  configured threshold, and a size limit enforced while reading. The byte-oriented
  `put` / `get` remain for callers that genuinely hold the whole payload.
  **Outstanding:** no production caller streams yet — the paths that would benefit
  (export packaging, archive shards) are phase 2. The contract tests are what keep the
  streaming path from rotting until then.
- Conditional create, provider generation metadata, retries, upload limits, error taxonomy,
  connection pooling/close hooks and readiness capability tests.
  **Conditional create, compare-and-swap, SDK retries, error taxonomy and a close hook are
  done. Provider version tokens are read for compare-and-swap but not yet surfaced on the
  port; upload limits and role-specific readiness (F15) are outstanding.**
- Add a verified resumable filesystem-to-cloud migration command and inventory report.
  **Done:** `lab storage migrate`, installation-wide, with a dry-run plan, a journal
  that resumes an interrupted run, digest verification by default, and a re-run that
  copies nothing and verifies everything as the acceptance pass. Identity is preserved
  by construction and creation time travels with the object. Tombstoned payloads are
  deliberately not copied — the denial lives in the database, which does not move —
  and are named in the report. Exercised end to end against PostgreSQL and two
  filesystem stores; filesystem-to-S3 is the same code through the same port, and
  has not been run against a live bucket.
- Add private portable backup/restore commands and manifests; test app/version/extension
  compatibility, volume name discovery, secrets references and one-writer cutover.
  **Done:** `lab backup create / verify / restore`, writing a recovery manifest
  ([schema](../../schemas/recovery/backup-manifest.schema.json)) that records the app
  version, Alembic revision, PostgreSQL and extension versions, object store kind and
  address, per-workspace counts, and the *names* of the secrets a restore needs. The
  payloads travel through the `ObjectStore` port, so a bucket backs up like a directory
  through the same verified copy `lab storage migrate` uses. Restore preflights before it
  touches anything and refuses a missing or older extension, an older PostgreSQL major, a
  revision this code has never heard of, and a backup that records a hole in itself.
  Volume names are resolved from the host by `infra/backup/lib/volumes.sh`, which refuses
  on no match, on ambiguity, and on a target that is not empty.
  **One-writer cutover is a refusal, not a warning:** a restore is a clone under a new
  identity unless the operator declares the origin fenced, and a clone may not publish.
  **Run against real PostgreSQL** (16.13 + pgvector 0.8.2, `pg_dump` 16.13): 985 passed,
  and the operator commands driven end to end — create, verify, a refused restore into a
  populated database, a refused restore into a database without pgvector, an applied
  restore into a fresh one, and `lab db verify-restore` clean afterwards. Recorded in the
  audit's section 5. **That run found two defects a reading would not have**: a logical
  restore needs an empty destination, and the restore had its two steps in the backup's
  order rather than its own. Both are fixed and both now have tests.
  **Outstanding:** nothing has been restored onto a *second machine*; backup encryption
  and key recovery are untested; neither end of a backup has been S3. Encryption at rest
  is deliberately the operator's storage, not this command.
- Repair unsafe schema/code rollback assumptions in deployment tooling before migrations.
- Keep existing filesystem profile and object UUIDs/citations backward-compatible.

Acceptance: move a populated synthetic installation to a new VM/container environment with
its DB dump and the remote bucket; no source acquisition, parsing or model extraction is
needed. Original citations and rights resolve. Restart/retry during transfer loses nothing.
S3/GCS credential rotation and denied permissions behave correctly. Production move remains
a deliberate operator action with a backup and verified counts.

**Stop here if the actual requirement is only easier VM replacement.** This is not yet an
independent reader feature and must not be advertised as one.

### Phase 2 — Durable research packages and publication protocol (large)

Work:

- Implement v1 contracts, canonical digest/signature format and transitive dependency
  enumerator. Reuse existing release concepts without assuming current export completeness.
- Create private paper checkpoints at expensive stage boundaries, plus a result lookup index
  so failed/empty/successful runs are distinguishable across restarts/installations.
- Freeze paper package revisions and snapshot catalog with cross-paper relations, templates,
  rights and origin actor provenance. Include raw source provenance selectively by entitlement.
- Export vectors/chunks with exact profile digest and portable numeric encoding, count/digest
  checks and safe validation (finite numbers, dimension, normalisation). Physical indexes
  are not portable evidence and are rebuilt locally.
- Implement staged publication/outbox reconciliation, atomic head compare-and-swap and
  writer identity/epoch. Preserve unchanged artifact/package digests incrementally.
- Add a complete catalog validator and a read-only inspect/dry-run CLI.
- Add signed policy/revocation checkpoints and retention dependency metadata.

Acceptance: archive fixture can be validated without the producer DB; deleting the producer
SQL projection does not lose published meaning. Faults before head commit are invisible;
fault after head commit is reconciled without duplicate publication. No publisher advances
head from a stale epoch. Interrupted stage checkpoint can be resumed without re-analysis
when its recorded completion is known.

### Phase 3 — Independent reader import and serving (large)

Work:

- Introduce installation capabilities and restricted runtime/importer DB roles.
- Implement resumable staging importer, source-workspace binding, approved group mapping,
  row counts, validation, indexes and atomic generation activation.
- Refactor corpus/operational access in accounts, chat, retrieval, analytics, citations and
  exports. Keep current policy filters inside SQL candidate queries.
- Reader composition removes acquisition/extraction/evaluation task dispatch, unnecessary
  connector/scanner dependencies and write-capable archive credentials.
- Expose role/last sync/release/policy freshness/compatibility status; disable unavailable
  corpus actions in UI and enforce the same restriction at API, CLI and worker layers.
- Private export scratch storage, local audit/chat backup and verified object cache.
- Old release retention/on-demand rehydration for existing chat citations; local rollback
  selects a scientific generation while enforcing current policy.

Acceptance: empty reader with GET-only S3/GCS access passes the end-to-end success definition
in section 1. Search/chat/login work; forged mutations fail at application and DB/storage
layers. The producer can be offline while the reader serves within the allowed policy
freshness window. A failed sync leaves the previous valid generation available.

### Phase 4 — Role handover and workhorse deployment (medium/large)

Work:

- Separate compute-worker placement from publisher authority. Provide Compose/service
  profiles for all-in-one, publisher/worker, reader and maintenance.
- Implement operator preflight, draining, verified checkpoint, credential fencing, epoch
  transfer, restore and enable steps with resumable local progress/audit.
- Reject promotion without producer working-state recovery or explicit accepted loss scope.
  Preserve existing reader operational data while adding producer state.
- Fence old queued/in-flight tasks and source budgets; provider requests with unknown outcomes
  are marked uncertain and reconciled rather than invisibly charged again.
- Configure network/DB TLS, model caches and worker-version compatibility explicitly.
- Document failover failure points and recovery ownership; do not auto-promote on heartbeat.

Acceptance: move writer authority from VM A to B with A still reachable and again with A
unreachable but externally fenced. A cannot publish or claim new work after demotion.
A stale completion cannot attach to B's current publication. Reader chats survive promotion.
All writer attempts and role changes have distinct attributable audit records.

### Phase 5 — Lifecycle, recovery, cost and scale hardening (medium; required before broad use)

Work:

- Implement reachability-based purge with dry-run reports, retention/hold/version handling,
  reader cache invalidation and separate deletion authority.
- Test backup restoration without the live original bucket, encryption-key recovery,
  AWS-to-GCS/GCS-to-AWS copy and descriptor remapping with stable logical IDs.
- Measure catalog/import/vector/index/citation latency, network failure behaviour, memory,
  archive cost growth, source/model calls avoided and policy propagation bounds.
- Automate disaster drills and document RPO/RTO from measurements. Optional warm projection
  checkpoint/managed DB decision if bootstrap misses targets.
- Add alerts for stale reader policy, failed import, orphan growth, wrong-role access,
  missing artifact, corrupt object, pending cold restore and unfenced writer conflicts.

Acceptance: the scenario matrix below is satisfied for the deployed feature set; exceptions
are documented as disabled capabilities rather than hidden limitations.

## 4. Existing files to inspect/change by responsibility

| Area | Existing implementation anchors | Planned work |
|---|---|---|
| Storage contracts/adapters | `application/ports.py`, `adapters/object_store/{filesystem,s3}.py` | Streaming/capabilities/CAS and GCS; registry integration |
| Runtime configuration | `config.py`, `services/api/src/lab_assistant_api/deps.py`, `infra/environments/` | Separate environment, backend, installation role and capabilities |
| Database | `adapters/db/{tables,repositories,engine,search}.py`, `database/migrations/` | Object registry, generations, import/publication ledger, least-privilege roles |
| Domain archive contracts | `contracts/release.py`, `contracts/canonical.py`, `contracts/errors.py` | New typed v1 archive/result contracts and named outcomes |
| Processing | `application/{discovery,acquisition,parsing,extraction,indexing}.py` | Persist reusable complete stage results and typed dependencies |
| Publication | `application/releases.py`, `application/export.py` | New archive publication service; preserve reporting export purpose |
| Provenance/evaluation | `provenance/fingerprints.py`, `evaluation/runner.py`, model adapters | Real input/version/result provenance; archived evaluation evidence |
| Auth/query | `application/{context,accounts,chat,retrieval,analytics}.py`, policy modules | Corpus versus operational access, freshness/current rights, role enforcement |
| Entrypoints | API `main.py`/`routes.py`, `cli/main.py`, worker entrypoints | Reader-aware readiness/UI/API/CLI; import and handover commands |
| UI | `apps/web/src/` | Role capability display, sync status and clear unavailable operations |
| Operations | `scripts/{deploy,lib/common}.sh`, `infra/backup/`, Compose files | Migration-safe updates, role-specific profiles, portable backups/restore |
| Documentation/tests | ADR index, deployment/backup/security/contracts docs and tests | Superseding decisions, reproducible runbooks, acceptance suite |

New modules should follow these boundaries, not accumulate in a route or an enormous CLI
function. Use fixed runtime image digests for portable execution; do not depend on a
researcher's unrecorded local Python environment or filesystem paths.

## 5. Scenario and acceptance-test matrix

Each row is a required scenario when the corresponding phase ships. Use synthetic data
including policy-denied work and assert negative behaviour as well as positive results.

| ID | Scenario | Required outcome |
|---|---|---|
| S01 | Existing single VM + new remote bucket | Same API behaviour, existing object IDs and citations preserved |
| S02 | Empty reader imports current library | Search/analytics/chat/citations work without source or batch model calls |
| S03 | Reader refresh to next release | All rows switch coherently; old in-flight queries keep pinned generation |
| S04 | No changes since last refresh | No duplicate rows, vectors or payload transfers; head/policy checks still happen |
| S05 | Interrupted payload/shard upload | Retry resumes/verifies; uncommitted snapshot is not visible |
| S06 | Interrupted import/index build | Old generation remains active; retry uses checkpoint safely |
| S07 | Cloud head committed, DB ack fails | Publication reconciles as already committed, no second release |
| S08 | Two publishers race one head | Only one conditional commit; loser reports conflict and does not merge silently |
| S09 | Stale writer after promotion | Old epoch cannot publish; old workers/credentials are fenced |
| S10 | Lost VM after completing unpublished extraction | Private checkpoint restores known completed result and pending review |
| S11 | Model call succeeds remotely, response lost | Uncertain outcome recorded; no claim of exactly-once cost; explicit retry policy |
| S12 | Successful extraction produces no claims | Completed empty result reused; no endless re-extraction |
| S13 | Failed/refused/truncated extraction | Typed outcome; retries only when appropriate; not mistaken for verified complete claims |
| S14 | Identical bytes acquired twice under different rights | Blob dedupes; distinct acquisition/rights evidence remains |
| S15 | Same DOI, changed corrected paper bytes | New asset/rendition/package revision; earlier citations stable |
| S16 | Title similarity or contradictory strong IDs | Review/conflict retained; no automatic merge on import |
| S17 | Merge or retraction crosses two paper packages | Dependency-complete snapshot; explicit supersession/retraction display |
| S18 | Existing user's old chat opens older release citation | Correct original evidence resolves, subject to current access/retention |
| S19 | Reader rollback to old release after rights revocation | Old scientific state does not restore revoked rights |
| S20 | Permission expires during offline period | Restricted serving stops at configured bound; public offline mode only if approved |
| S21 | Membership removed while evidence is cached | Next request denies; cache hit never bypasses access checks |
| S22 | Unknown source group/actor on import | Group access denied until mapped; actor attribution never grants login/admin |
| S23 | Reader receives forged mutation via API/CLI/task | Explicit role denial, no shared PUT, no corpus SQL writes |
| S24 | Reader login/chat/feedback with bucket GET-only | Local writes succeed; source/worker dependencies not required |
| S25 | Reader builds policy-permitted export | Private/local stream only; no shared archive mutation |
| S26 | Reader unavailable UI action POST (including search) | Search still works; action allowlist is semantic, not blanket POST ban |
| S27 | Bucket returns 403 | Permission error; no miss/reacquisition/write fallback |
| S28 | Required object 404 versus optional withheld artifact | Required fails validation; optional reason displayed without fabricating content |
| S29 | Corrupt/truncated content or manifest | Hash/length/signature failure, no activation or citation from bad bytes |
| S30 | Credential expiration mid-transfer | Refresh/resume within limits; no secrets logged; revoked identity remains denied |
| S31 | Custom endpoint port / encoding / region / signed URL | Real-provider or standards fixture signatures accepted; exact canonical request tested |
| S32 | Reader has GET but no broad bucket LIST | Role-specific readiness succeeds using permitted known metadata |
| S33 | Malware/quarantined or unapproved upload | Not published as servable; quarantine restricted to producer |
| S34 | Malicious archive (path traversal, decompression bomb, SQL/pickle) | Safe data-only parse rejects before writing outside staging or exceeding quotas |
| S35 | Newer incompatible format/schema | Refuse activation, retain old compatible generation, explicit upgrade message |
| S36 | Same vector dimensions, different model/revision | Reject incompatible profile; dimension alone is not compatibility |
| S37 | Embedding profile unchanged | Import vector values, zero document re-embedding; physical index build allowed |
| S38 | Query embedder unavailable | Explicit unavailable/degraded lexical mode per configuration; no silent fake model |
| S39 | Restricted vectors/chunks imported | Pre-ranking SQL and counts obey same policy as original text |
| S40 | Old/new library snapshots share payload | Deleting one reference does not remove other live references |
| S41 | Purge requested with retained versions/holds/backups | Explain remaining retention; separate privileged purge with verified scope |
| S42 | Stale tombstone update races new registration | Conditional conflict; no resurrection by ordinary put |
| S43 | Cache disk full or cache eviction | Bounded failure/fetch behaviour, no corrupt cache publish or database loss |
| S44 | Bucket outage while serving cached evidence | Serve only policy-valid complete data; missing evidence fails clearly |
| S45 | Archived cold object needed for citation | Named restore-pending state; do not claim missing or start re-analysis |
| S46 | Zscaler user reaches VM but not storage endpoint | Authorised proxy download or clear failure; no unsafe public bucket fallback |
| S47 | Database restored to earlier point than archive | Verify references; tolerate unreferenced extra immutable blobs; current revocations retained |
| S48 | Database newer than available archive recovery set | Verification blocks serving missing required evidence |
| S49 | New VM/OS/PostgreSQL version | Supported logical restore and migrations; extension versions checked |
| S50 | Different Compose project/volume names | Restore targets resolved and fresh; no guessed destructive volume operations |
| S51 | Cloned DB contains leased tasks/source cursors | Old writer fenced; leases/completion reconciled; no duplicate uncontrolled source calls |
| S52 | Reader promoted while it has local chats/users | Private operational state preserved; producer state imported separately |
| S53 | Old publisher unreachable and cannot be fenced | Promotion blocked; policy-valid readers continue |
| S54 | Two compute VMs share publisher DB | Stage caps/rate budgets coordinate; no increase just from replica count |
| S55 | Full library exceeds 1,000 releases or large shard count | Keyset pagination verifies/imports every row; counts are complete |
| S56 | Non-default chunker and normalisation versions | Round-trip manifest and evidence verification preserve exact semantics |
| S57 | AWS-to-GCS provider move | Stable logical IDs/digests, verified mapping, old source retained until cutover passes |
| S58 | Backup storage or encryption key unavailable | Recovery fails clearly; test key recovery and independently held copies |
| S59 | Old app after forward DB/archive migration | Compatibility gate blocks unsafe deployment rollback |
| S60 | Reader modifies head or accesses producer backup | IAM denial even if app check is bypassed |
| S61 | Scientific review policy changes after extraction | Reuse bytes/results only if compatible; renewed review/gates as required |
| S62 | Multi-lab library import attempted in v1 | Explicit unsupported/mapping-required result; no silent cross-workspace reuse |
| S63 | Import uses no external source/model network access | Assert provider/parser call counts zero for completed artifact reuse |
| S64 | Provider object ETag differs from content SHA | Application hash verified independently; no false corruption or false trust |
| S65 | Reader restarted with stale/replayed catalog head | Monotonic sequence/pinned trust checks prevent unintended rollback |
| S66 | Publisher-signing key rotation | Trusted transition verified; unknown key cannot activate data |

Additional operational tests: cold restart of every service, graceful shutdown during
publication/import, schema migrator privilege separation, database pool exhaustion, audit
append contention, time skew/credential expiry, partial backups, retries after timeouts,
source rate-limit persistence, and quota/retention cost alerts.

## 6. Migration from today's filesystem store

A mere change of backend config is explicitly insufficient. Build a dedicated command with
these stages and a persisted progress report:

1. Inventory DB references and the filesystem JSONL registry; identify orphan records,
   missing/corrupt payloads, tombstones, conflicting metadata, unresolved object classes and
   all referenced published/draft objects. Inspect entire data, not only active release.
2. Take protected DB and complete object-volume backups. Measure counts/bytes and establish
   free space and destination permissions without printing credentials.
3. Bulk copy immutable content while the old store remains primary if supported. Preserve
   object IDs, SHA, byte length, media type, class and creation metadata. Never recompute IDs
   under a new workspace. If v2 namespacing changes keys, record verified mappings.
4. Reconcile/recreate destination metadata and SQL registry after verifying bytes. Do not
   rely on copying payload keys without the `records/` metadata that the legacy adapter needs.
5. Enter maintenance, drain API mutations/workers, freeze tombstone/purge changes, then copy
   the delta and consistent final registry. A POSIX lock/consistent snapshot is needed for
   a live JSONL registry; copying the file while appending is not a complete protocol.
6. Verify every required digest and count; run restore/dependency checks. Validate destination
   policy and encryption. Persist a migration manifest including missing optional items.
   **Implemented as the journal plus the report's `plan` block.** Destination policy and
   encryption validation are still outstanding: the command checks bytes, not bucket
   configuration.
7. Change all API and worker backend settings coherently, start in restricted validation mode,
   run representative search/citation/download checks, then enable serving/workers.
8. Retain old store read-only until successful acceptance and backup retention requirements
   are met. If new remote writes occur after cutover, rollback needs a reverse delta or
   recovery checkpoint; simply flipping the config back loses those objects.

Prefer single-primary cutover over indefinite dual writes. If online dual operation becomes
necessary, specify reconciliation and failure states explicitly; do not bolt on a best-
effort mirror inside `put` and call it durable.

After this migration, phase 2 publishes archival graph snapshots. Filesystem-to-S3 migration
and scientific-graph-to-portable-library publication are different operations.

### Local-state relocation inventory

Before moving any installation, generate and verify an inventory rather than assuming the
object bucket is the complete application:

| Local item | Move/rebuild action |
|---|---|
| PostgreSQL named volume, roles, extensions, WAL/backup configuration | Verified logical dump plus role/version/extension manifest; physical recovery only within its supported compatibility envelope |
| Legacy objects volume and JSONL registry | Copy/verify until all references are migrated; retain rollback inventory |
| `.env`, address files, external configuration and signing/key references | Recreate securely from secret/config management; preserve deliberate auth continuity or explicitly revoke old sessions |
| Nginx site, port/base-path routing, TLS chain and private key references | Recreate or reissue for destination; test public hostname, proxy headers and certificate trust |
| Embedding/model cache and pinned model weights | Optional copy/mirror; verify digest and query-profile compatibility; do not assume weights will remain downloadable |
| ClamAV signatures/socket volumes | Rebuild socket; update signatures; validate scanner version/status before processing new inputs |
| Import/checkpoint journals and private export artifacts | Persist resumable state in DB/private storage or include verified local files in backup |
| Container images/code/package lockfiles | Pin image digests and source revision; maintain reproducible build inputs |
| Logs, temporary files, support bundles | Retain only required diagnostic records; disposable files are not corpus authority |
| Host timers/service units, mount ownership/SELinux labels, network rules | Recreate from documented provisioning; validate rather than copy host-specific paths blindly |

Copying a DB must not copy authority to run a second publisher. The recovery manifest
identifies the old installation; activation allocates or deliberately restores identity
under the handover protocol. Backups contain sensitive account hashes/research data even
when `.env` is excluded, so private encrypted backup storage is mandatory for this design.

## 7. Test and measurement strategy

- Unit: canonicalisation, package dependency traversal, fingerprints, roles, state machines,
  version compatibility, current-rights overlay, writer-epoch transitions.
- Adapter contracts: memory/filesystem/S3/GCS same semantics; condition conflicts, tombstones,
  integrity, timeout/403/404/429/5xx, expired identity, streaming abort, multipart cleanup.
- SQL integration: real PostgreSQL with pgvector and FK constraints; migrations from current
  populated schema; restricted roles; atomic generation activation and audit concurrency.
- Process integration: separate producer and reader processes, independent DBs and credentials,
  shared synthetic remote archive; no in-memory shared state between installations.
- End to end: publish/import/search/ask/citation/feedback/private export/refresh/revoke/move.
- Opt-in live-provider suite: ephemeral synthetic prefix/bucket only; temporary identities,
  actual denied mutations, native GCS generations and S3 conditional updates. Emulator-only
  results must not be reported as cloud validation.
- Recovery drill: destroy the disposable test producer/reader databases and restore from
  recorded archive/private backups; prove which unpublished work is recovered and which
  is within the declared RPO.

Record baseline and target numbers for corpus bytes, graph rows, vectors/dimensions, shards,
publication duration, first import, incremental refresh, pg_dump/restore, index build,
p95 search/citation latency, cache hit rate, policy propagation, model/source calls avoided,
and actual monthly storage/request/egress cost estimates. Set RTO/RPO only after the operator
chooses targets and these numbers are measured. No invented "instant" migration guarantee.

## 8. Operator-facing deliverables

Proposed command families (not currently runnable):

```text
lab storage inspect / migrate / verify
lab archive package / publish / inspect / verify
lab archive sync --dry-run / sync / status
lab installation status / drain / promote / demote
lab backup create / verify / restore
lab archive retention plan / apply
```

Each command needs machine-readable output, nonzero failure codes, a dry-run where relevant,
explicit library/installation target, progress/resume ID, redacted logs and an audit event.
Destructive operations need a concrete reviewed plan and target verification. Ordinary
sync should not ask for confirmation on every shard. Scripts must resolve configuration
rather than require operators to paste long lists of commands or secrets.

Documentation additions: fresh cloud storage setup, native/Compose profile matrix,
filesystem migration, reader bootstrap, role handover, recovery, policy freshness/outage,
provider transfer, retention/purge, credentials/KMS rotation and upgrade compatibility.
Link from the main docs index and retain clear accepted/proposed status.

## 9. Decisions to confirm with user/operator before deployment

| Decision | Recommended default / why it matters |
|---|---|
| First cloud provider and region | Use the institutionally supported service near compute; implement/test one first if schedule requires |
| Same lab versus multiple labs | Same library/workspace first; cross-lab transfer needs explicit entitlement/mapping |
| Independent readers truly required? | If only UI/compute separation is needed, option B is substantially simpler |
| Acceptable refresh delay | Minute-scale polling as a starting proposal; no guaranteed number until tested |
| Reader features | Search, chat, citations, feedback, policy-permitted private exports; curation on publisher |
| Policy freshness/offline use | Short bounded validity for restricted content; public offline mode only explicitly approved |
| Draft/result backup RPO | Decide tolerable loss of unpublished expensive analysis; checkpoint frequency follows |
| VM replacement RTO | Measure DB restore/indexing; use prewarming/remote DB if target is tighter |
| Publisher handover | Operator-driven fencing, one writer; no automatic failover initially |
| Credentials and KMS ownership | Institution-managed identities, separate backup/purge authority; documented key recovery |
| Model-result retention and rights | Decide which raw requests/results/artifacts may be stored remotely and reused |
| Library version/history retention | Balance stable citations, correction history, licence expiry and storage cost |
| Existing reader promotion | Preserve local user/chat state, recover private producer graph; do not overwrite reader DB |

No decision here authorises cloud spending, moving licensed files, changing production IAM
or deleting existing storage. Those are concrete later deployment actions; this session
records architecture and an implementation-ready plan only.

## 10. Completion ledger for future sessions

- [x] Recheck audited HEAD and current deployment inventory. HEAD is still `927c5ed`;
      F02, F03, F04 and F05 were reproduced from the code before any change.
- [ ] Phase 0 correctness repairs and real PostgreSQL evidence. **In progress.**
      - [x] F02 object registration, SQLite foreign keys, AST guard and a lifecycle
            integrity test over a real database (`tests/integration/test_object_registry.py`).
      - [x] F16 PostgreSQL test isolation. The PostgreSQL suite had never run: fourteen
            duplicate-key failures, because one shared database plus a deterministic id
            factory gives the second test the first test's UUIDs. `reset_database` in
            `testing.py` fixes it.
      - [x] Real PostgreSQL evidence on 16.13 + pgvector 0.8.2: 780 passed (761 offline),
            `alembic upgrade head` on empty and populated databases, and the lifecycle
            against the Alembic-built schema. Recorded in the audit's section 5.
      - [x] F17 `alembic check` drift fixed: the three expression indexes are declared
            with `.ddl_if(dialect="postgresql")`, the check is clean against a fresh and
            an already-migrated database, and a contract test fails if a later migration
            adds a raw-SQL index the metadata does not describe.
      - [x] Backfill and orphan reconciliation: `lab storage scan` / `lab storage
            backfill`. **Correction:** dangling references cannot exist on PostgreSQL —
            the foreign key refuses the insert — so a deployed database has not been
            writing them, it has been failing those writes. Backfill is for a restore
            without constraints; orphan payloads are the state a deployed store may
            actually hold.
      - [x] F03/F04/F05 S3 adapter replaced with the maintained SDK
            ([ADR-0023](../architecture/adr/0023-object-storage-sdk.md)): error taxonomy,
            credential chain, conditional create and compare-and-swap, shared-bucket
            prefix, 26 stubbed contract tests. **No live-provider request made:**
            `tests/integration/test_s3_live_endpoint.py` is written and unrun, because
            this machine's Docker could not pull a MinIO image. Run it before any
            deployment depends on presigned URLs or conditional writes.
      - [x] F12 `verify-restore` reachability, pagination and strict manifest round
            trips. Keyset-paged, sweeps all 12 reference sources and re-digests every
            object; a manifest that will not parse is a finding, not an escape. The
            non-default-chunker round-trip defect this predicted was real and is fixed.
      - [x] Golden portable archive fixture: `lab_assistant.fixtures.archive`
            publishes two releases containing a revised paper, restricted evidence,
            review history, a searched-and-reported-nothing extraction and reusable
            projections. Each case is asserted, so a fixture that stops producing
            one fails rather than quietly narrowing phase 2's coverage.
      - [x] Portable archive schema draft: `contracts/archive.py` plus committed
            JSON Schemas under `schemas/archive/`, marked `1.0.0-draft`. Nothing
            publishes or imports them yet — that is phase 2 — but the format and the
            fixture are now designed against each other.
      - [x] Phase 0 restore acceptance gate: the check catches missing original
            bytes, a missing rendition, missing claims and config, a missing
            projection, and corruption past the thousandth release. Three of those
            were gaps the gate itself exposed and are now closed.
- [ ] Phase 1 backend(s) and portable single-installation recovery accepted.
      **In progress.**
      - [x] F13 portable backup/restore commands and manifests. `lab backup create /
            verify / restore`, a committed manifest schema, compatibility preflight,
            secret references by name, host-resolved Compose volumes, and the
            one-writer rule as a refusal. The predecessor's scripts backed up a
            filesystem store with `rsync` — which is not a backup of a bucket — and
            assumed every compatibility question a restore needed to ask.
      - [x] Installation identity (`LAB_INSTALLATION_ID`), derived from the
            installation's own addresses when unset so a restore onto a new host
            correctly derives a different one. The manifest records which case applies,
            because a derived identity does not survive a deliberate address change.
      - [x] F15 readiness. `/ready` now answers **503** when it should not be sent
            traffic; it returned 200 with `"status": "degraded"` in the body, so every
            load balancer and container healthcheck kept routing to a process that had
            just reported its dependencies down. It also checks the migration revision
            against the one this build expects — an API container that starts before
            its migrations passed every check and failed on the first real query — and
            it no longer proves the object store works by writing to it once per probe.
            A check that cannot measure something reports "unknown" and does not block:
            a probe that fails closed on its own blindness is an outage.
            **Role-specific checks are now included**, once F07 made them possible: a
            reader fetches the manifest of the release it serves instead of listing the
            bucket, because the credential it should have cannot list; and a reader with
            nothing servable refuses traffic while a publisher in the same state stays
            ready, since a publisher without a release yet is still being set up.
            A publisher's write preflight is deliberately *not* on the probe — readiness
            runs on a timer, and a write on a timer is the defect this removed — and the
            policy-age check has nothing to measure until phase 3 writes policy
            checkpoints.
      - [x] Upload limits. The ceiling was enforced only while streaming, so anything
            holding a whole payload — the upload route does — went straight past it.
            `put` now enforces it on all three stores, and the filesystem store, which
            is the deployed profile, had no ceiling at all.
      - [x] F07 installation role. `LAB_INSTALLATION_ROLE=publisher|reader`, enforced
            at composition, the application services, every mutating route and task
            claims, with a contract test that fails when a new mutating route declares
            no capability. A reader needs no source API keys and no scanner, refuses to
            start a worker, and still searches, chats, records feedback and exports —
            the role is about changing the corpus, not about writing to a database.
            This also unblocks the role-specific half of F15.
            **Still owed:** least-privilege SQL and cloud credentials per role. The
            application refuses the operations; the database role a reader connects as
            is still the publisher's.
      - [x] GCS adapter ([ADR-0024](../architecture/adr/0024-native-google-cloud-storage.md)).
            Native, not S3 interoperability: that endpoint has neither generation
            preconditions nor resumable uploads, so the conditional writes this
            application rests on would silently degrade to check-then-put.
            The earlier objection was that it could not be tested; that turned out to
            be wrong. `fsouza/fake-gcs-server` runs locally, so it ships with the same
            two layers S3 has — 33 offline contract tests over a fake client, which
            carry CI, and 11 against a real GCS HTTP implementation, of which 10 pass
            and one skips.
            **Two provider differences, documented rather than papered over:** GCS has
            no SHA-256 checksum field, so the provider verifies MD5 and this
            application's SHA-256 remains the identity check on every read; and a V4
            signed URL needs a service-account key or IAM `signBlob`, so `signed_url`
            raises where neither exists rather than handing out a URL that 403s.
            **Not proven by an emulator:** IAM behaviour, Google's own signing, and how
            a real bucket's retention and lifecycle rules interact with a tombstone.
            The signing test skips with that reason rather than asserting a pass.
      - [ ] Provider version tokens surfaced on the port. **Deferred to phase 2 on
            purpose.** They are read for compare-and-swap today and nothing consumes
            them; the publication protocol is the only caller there will be. Adding
            unexercised surface now is the staged control that rots.
      - [ ] A recovery drill: restore into a scratch VM and record date, duration and
            what went wrong. The commands are exercised end to end against real
            PostgreSQL, on one machine, into scratch databases — which is not the same
            as a VM rebuild, and the RTO remains unmeasured.
      - [ ] A backup with S3 at either end, and tested encryption-key recovery.
      - [x] First live-endpoint evidence for the S3 adapter. Seven of the nine
            behaviours in `tests/integration/test_s3_live_endpoint.py` now have a real
            provider behind them, including a presigned URL that actually fetches its
            bytes — the defect class that broke the predecessor's hand-written signer,
            and the one no stub can catch — and `If-None-Match` honoured on a
            conditional create. **The two that assert a refusal are skipped, not
            passed:** the emulator available here accepts any signature and any
            credential, so it cannot demonstrate a rejection. They name what went
            unproven and run as soon as the endpoint enforces signatures. Run them
            against the institutional gateway before S3 carries production evidence.
- [ ] Phase 2 complete archival format and crash-safe publication accepted.
      **In progress.**
      - [x] Catalog contracts and the transitive dependency enumerator.
            `contracts/archive_catalog.py` defines twelve typed record kinds, aligned
            field by field with the domain they are built from — the first draft was
            written from memory and had a title on `Work` (it is reconciled from source
            assertions), a normalisation version on `Rendition` (it is on the span), a
            string locator (it is structured) and a name that collided with
            `contracts/release.py`. `application/archive_catalog.py` walks a release's
            closure rather than dumping tables: release → entries → works →
            manifestations → assets → rights decisions → renditions → spans, and
            entries → pinned claim revisions → claims → reviews, following
            `supersedes_revision_id` backwards so the reason a number changed travels.
            Shards are canonical JSONL, sorted by id, digested over the bytes, so two
            builds of one closure are byte-identical — which is what a later incremental
            publication rests on.
      - [x] **First acceptance gate reached: the archive fixture validates without the
            producer DB.** `application/archive_validate.py` takes a directory and opens
            nothing else. It checks the manifest digest, every shard's length, digest
            and row count, every row against its record model, and — the check that
            distinguishes an archive from a table dump — that every reference resolves
            *within* the snapshot. The golden fixture passes with zero blocking
            findings; the two advisories are real (its releases carry no scope version).
      - [x] `lab archive package / inspect / verify`.
      - [x] Projection export, and with it **the fourth acceptance gate: deleting
            the producer SQL projection does not lose published meaning.** The test
            does it rather than asserting it — drops every chunk and vector from the
            producer's index, then validates the snapshot with no database.
            Vectors travel as base64 IEEE-754 float32 little-endian, named in every
            row rather than assumed. JSON floats do not round-trip reliably across
            parsers, and this repository already refuses floats in fingerprint inputs
            for that reason. float32 is what the vector store holds — pgvector's
            `vector` is a 4-byte float — so this is lossless against PostgreSQL; a
            vector that has only lived in memory as a Python double is narrowed by
            exactly as much as writing it to the database would narrow it, and the
            contract says so rather than claiming a bit-exact copy of a float64.
            **The index does not travel.** `embedding_profile_digest` covers model,
            revision, dimensions and normalisation and deliberately excludes
            `index_kind` and `index_parameters`: a reader builds its own index, and
            folding those in would have it re-embed a corpus because the producer
            preferred a different `ef_construction`.
            Validation decodes every vector and checks dimension, finiteness and
            l2 norm, and checks each chunk against its own content digest. A NaN in a
            similarity search does not raise — it makes a passage unreachable or ranks
            it first, depending on the backend.
            `SearchIndex` gained keyset-paged `list_chunks` / `list_embeddings`:
            serving never needed to read a projection back, which is why they were not
            there.
      - [x] Publication protocol: outbox, staged publication, atomic head
            compare-and-swap, writer epoch. `lab archive publish / reconcile` over an
            `ArchiveControlStore` port with filesystem, S3, GCS and memory adapters —
            the preconditions each backend already had, surfaced for the head: an ETag,
            a generation, and a digest under an exclusive lock (single-host, which is
            what ADR-0022 scopes the filesystem profile to).
            **Three acceptance criteria met**, each with a crash test that stops calling
            rather than patching in an exception: a fault before the swap leaves objects
            nobody references and the head unmoved; a fault after it is reconciled to
            `acknowledged` without a second sequence; and a publisher whose epoch is
            behind the head is refused. The refusal tells the operator **not** to re-run
            with the epoch the head just reported — the architecture proposal names that
            as the way a fenced writer takes the library back, and it would work.
            A failed swap is never retried, for the same reason.
            The outbox is `archive_publications`, added by a **conditional** migration:
            `0001_initial` builds from live `MetaData` with `checkfirst=True`, so a
            database created today already has the table and one migrated earlier does
            not. That asymmetry is F14, and until `0001` is pinned to a frozen schema
            every later revision has to be idempotent. Verified both ways against
            PostgreSQL 16: fresh and pre-existing converge, `alembic check` clean.
      - [x] Stage result index, and with it **F10 fixed**. Reuse was decided by
            looking for *output*: extraction asked whether a claim revision carried
            the right fingerprint, so a paper the extractor read and found nothing in
            produced no revision, looked unprocessed, and went back to the model on
            every restart — forever, for every silent paper. Nothing failed and
            nothing was logged; the provider was simply paid again.
            `stage_results` now records a typed `StageResultEnvelope` whichever way a
            stage ends, and `domain/stage_results.py` decides reuse from the outcome:
            succeeded, empty, truncated, refused-by-policy and permanently-failed are
            reusable; **retryable failures and unknown outcomes never are**, because a
            timeout recorded as a completed stage is how a transient blip becomes a
            permanent hole. A reused permanent failure is surfaced *as* a failure.
            **"Found nothing" is not "returned nothing":** a complete search over a
            paper reporting none of the targets comes back as a full set of
            abstentions, so the classification reads the abstention reasons rather
            than counting proposals. On the golden fixture this is the difference
            between four recorded successes and two successes plus two
            `succeeded_empty`.
            Proved by counting: a second pass over the corpus reaches the extractor
            only for the one rendition that had never been extracted.
            **Outstanding:** envelopes are indexed locally and not yet archived, so
            reuse does not cross installations — the plan asks for that and it needs
            the private checkpoint storage of phase 3. `error_class` was added to the
            envelope contract and its committed schema regenerated.
      - [ ] Signed policy/revocation checkpoints and retention dependency metadata.
      - [x] Manifest signatures. Ed25519 — deterministic, so there is no per-signature
            nonce to leak a key through. `lab_assistant.adapters.signing` signs and
            verifies; `domain/signing.py` decides whose key counts.
            **Keys are pinned in configuration, never learned from the archive**: a
            key fetched from beside the manifest is a key the manifest's author chose,
            and trust has to enter from outside the artifact being trusted. Rotation
            is an operator listing both keys for the overlap, which is an
            authenticated transition because changing configuration is one.
            Unsigned is advisory so signing can be adopted incrementally, and
            `LAB_ARCHIVE_REQUIRE_SIGNATURE` makes it blocking; a signature that *fails*
            to verify always blocks, as does one in an algorithm this version cannot
            check — "I cannot check this" must not read as "this is fine".
            **A correction worth recording:** the signed statement restates the
            library, snapshot, installation, epoch and sequence, and the first write-up
            of this claimed that was what stopped a replay. It is not. Those fields are
            already inside the manifest, so `signing_digest()` covers them and the
            digest alone defeats the replay. Reducing the statement to a bare digest
            and watching every replay test still pass is how that was found. The
            restatement is kept as insurance against a later version narrowing the
            digest, and `test_the_signing_digest_covers_identity` is the test that
            fails if one does — verified by narrowing it.
            `cryptography` was present transitively and is now a declared dependency.
- [ ] Phase 3 independent reader end-to-end accepted with GET-only cloud identity.
- [ ] Phase 4 role handover/workhorse tests accepted.
- [ ] Phase 5 lifecycle/disaster/scale drills accepted.
- [ ] Relevant ADRs accepted and current documentation reconciled with implementation.
- [ ] User/operator decisions, measured RPO/RTO and deployed feature/provider matrix recorded.

Suggested next-session prompt:

> Implement the shared archive plan in `docs/plans/shared-archive-implementation-plan.md`
> for Lab-AI-Assistant. Read its architecture and audit companions first. Start with phase 0
> and phase 1, preserve current filesystem deployments, and report progress against the
> acceptance gates. Do not claim independent reader support until its complete end-to-end
> tests pass. Recheck current Git state and do not access production data for tests.
