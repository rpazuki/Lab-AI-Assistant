Status: accepted
Owner: technical owner + operator
Last verified: 2026-11-17
Applies to: the data plane

# ADR-0023: Object storage through the maintained SDK

Supersedes [ADR-0017](0017-object-storage-adapter.md). The key convention, the
two key spaces, digest-verified reads, validation at construction and presigned
GETs are carried forward unchanged; only the transport and the signing change.

## Context

ADR-0017 chose a hand-written SigV4 signer over `httpx` rather than take a large
synchronous SDK into an async application. It was an honest trade with an
explicit revisit trigger, and it named its own condition: revisit "if the signing
code ever needs a feature SigV4 makes awkward."

Three things arrived at once.

**The signer was wrong in four independent ways**, found by reading it rather
than by running it:

- `_presign` computed the signature and *then* appended `x-lab-expires-at` to the
  query. A presigned URL's signature covers its query string, so every presigned
  URL this adapter produced would be rejected by AWS.
- `_sign` and `_presign` took the host from `httpx.URL(endpoint).host`, which
  drops the port. Against an institutional gateway on a non-default port the
  signed `host` header did not match the request's authority.
- The canonical URI ignored an endpoint path prefix, so any endpoint served under
  a path signed the wrong resource.
- `S3StoreConfig` accepted static access and secret keys only: no session token,
  no role, no refresh.

**ADR-0017 claimed test coverage that does not exist.** It described the signing
code as "exercised by adapter contract tests against a stubbed transport."
Searching this checkout for `S3ObjectStore`, `MockTransport` or `presign` finds
construction assertions in `test_production_container.py` and nothing else. The
eighty lines were unverified, which is why four defects could sit in them.

**The shared-archive work needs features SigV4 makes awkward** — the trigger,
arriving on schedule. Publication needs conditional writes (`If-None-Match` to
create, `If-Match` to compare and swap) so one publisher can advance a catalog
head and a stale writer cannot resurrect a tombstone. Long acquisition runs under
temporary credentials need a session that refreshes mid-job. Cold storage classes
need their own answer, distinct from "missing".

A fourth defect was behavioural rather than cryptographic, and did the most
damage: `_request(..., allow_missing=True)` returned `None` for 403 and 404
alike. A reader whose bucket permissions were wrong saw "this object does not
exist", and the honest response to a missing paper is to acquire it again — from
a source, for money, for bytes already sitting in the bucket.

## Decision

Implement `S3ObjectStore` over `boto3`, bridged to async at one explicit thread
boundary, and supersede ADR-0017's transport decision.

- **One thread boundary, bounded.** Every SDK call goes through `_in_thread`
  behind a semaphore. The boundary is in one place because a blocking SDK call
  issued from the event loop stalls every other request in the process, and
  presents as a latency mystery rather than an exception.
- **Credentials come from the chain.** Static keys remain supported; leaving them
  unset selects environment, shared config, container and instance roles, and web
  identity, with the SDK's own refresh. Construction still refuses when the chain
  resolves to nothing, so ADR-0017's "fail on the deploy, not on the 2am
  acquisition run" property is kept. Half a key pair is refused outright, because
  it would otherwise fall through to the chain and succeed as somebody else.
- **Failures keep their meaning.** Missing, denied, restore-pending, conflict,
  throttled, unreachable, timed out, unsupported and corrupt are nine outcomes,
  not one `None`. `ErrorClass.STORAGE_DENIED` and
  `ErrorClass.STORAGE_RESTORE_PENDING` join the taxonomy with their retry
  policies. The error *code* is read before the HTTP status, because a cold
  object answers `InvalidObjectState` with a 403 and a status-first reading turns
  "wait for the restore" into "you are not allowed to see this".
- **Writes carry preconditions.** Content is created with `If-None-Match: *`; a
  precondition failure there is a no-op, since the key is the digest. A record
  update carries `If-Match` on the version it read, so a tombstone cannot be
  undone by a slow writer holding an older copy. An endpoint that rejects
  preconditions is configured off deliberately — `object_store_conditional_writes`
  — rather than being discovered through a silent fallback.
- **Addressing is not identity.** An optional prefix isolates a shared bucket and
  is prepended to every key, but never stored: `StoredObjectRecord.key` stays
  logical, so the database survives a move to another bucket, prefix or provider.
- **Payloads stream, and the limit is a control.** `put_stream` spools to a
  digest — in memory while small, on disk once not — and refuses while reading
  rather than after, because a limit checked at the end has already allocated
  what it was meant to prevent. Above the multipart threshold the upload goes in
  parts, so a failed transfer resumes at a part boundary. `stream` yields chunks
  and verifies at the end, which is the only time it can: the contract is that a
  stream which raises has delivered bytes the caller must discard.
- **The provider checks the bytes too.** Every single-request write carries
  `ChecksumSHA256`, and multipart parts carry per-part SHA-256, so a damaged
  upload is rejected at the door rather than discovered on a later read.
- **The ETag is not the content hash.** It is a concurrency token. It is not the
  SHA-256 of a multipart upload and not the SHA-256 under server-side encryption,
  so the application digest is still verified on every read.
- `boto3` returns as an `objectstore` extra, and joins `dev` so the contract tests
  ADR-0017 described are the ones that actually run.

## Consequences

- The signing code is no longer ours, and neither are its bugs. Credential
  refresh, retry with jitter, multipart upload and regional endpoints come with
  it rather than being the next four things to hand-write.
- A sync SDK now sits behind an async facade. The thread boundary is real cost
  under heavy concurrent I/O, and it is bounded and in one function rather than
  spread across call sites.
- Dependency weight that ADR-0017 declined: `boto3` and `botocore` are large. The
  judgement has changed because the alternative turned out to be maintaining a
  signer whose defects could only be found with a real provider.
- The same thread-bridging shape fits `google-cloud-storage`, which is also
  synchronous, so a native GCS adapter is a second implementation of a pattern
  rather than a second architecture.
- Any S3-compatible endpoint still works, with one honest caveat ADR-0017 did not
  have to state: conditional writes are not universal across gateways. A
  deployment on one that lacks them sets the flag and accepts the race.

## Alternatives considered

**Repair the four signing defects.** Cheapest in the short run and rejected on
verification, not on effort: none of the four can be confirmed fixed without a
real provider, and the presigned-URL defect in particular is invisible to any
test that does not have AWS validate the signature. Repair would also leave the
credential chain, conditional writes and cold-class handling still to build.

**`aiobotocore` or `aioboto3`, natively async.** Removes the thread boundary.
Rejected for now: both pin narrow `botocore` ranges and lag upstream releases,
which is a poor trade for a research platform that will sit on one version for a
long time. `botocore.stub.Stubber` also gives better offline test fidelity than a
mocked transport. Revisit if the thread boundary ever shows up in a measurement.

**Keep `httpx` and sign with a maintained signing library.** A smaller
dependency, but the credential chain, the refresh and the retry behaviour are the
parts that matter here, and a signing library supplies none of them.

## Validation / revisit trigger

Revisit if the thread boundary appears in a latency measurement under real
concurrency, if a required provider has no SDK, or if a native-async SDK reaches
the maintenance level `botocore` has.

What this ADR does **not** claim: no request has been made to a live provider.
The contract tests run against `botocore.stub.Stubber`, which validates request
parameters and error shapes offline — it never signs a request and never parses a
real response, which is precisely the gap the replaced signer's presigned-URL
defect lived in.

`tests/integration/test_s3_live_endpoint.py` closes that gap and is written but
unrun: it needs an endpoint this session could not obtain. Run it against MinIO,
a gateway or a scratch bucket before any deployment depends on presigned URLs,
conditional writes, or denial behaviour:

```bash
LAB_TEST_S3_ENDPOINT=http://localhost:9000 LAB_TEST_S3_BUCKET=scratch \
LAB_TEST_S3_ACCESS_KEY=... LAB_TEST_S3_SECRET_KEY=... \
  .venv/bin/pytest -q tests/integration/test_s3_live_endpoint.py
```

A pass is evidence about the endpoint it ran against. MinIO passing is not AWS
passing, and credential refresh mid-job is not covered by either.

## References

- [Shared archive architecture](../../plans/shared-archive-architecture.md) §11
- [Code audit](../../plans/shared-archive-code-audit.md) F03, F04, F05
- [AWS S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- `packages/python/lab_assistant/adapters/object_store/s3.py`
- `tests/contract/test_s3_object_store.py`, `tests/integration/test_s3_live_endpoint.py`
