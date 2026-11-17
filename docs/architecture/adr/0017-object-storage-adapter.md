Status: superseded
Owner: technical owner + operator
Last verified: 2026-11-17
Applies to: the data plane

# ADR-0017: S3-compatible object storage, signed without an SDK

> Superseded by [ADR-0023](0023-object-storage-sdk.md), which replaces the
> hand-written signer with the maintained SDK. The key convention, the two key
> spaces, digest-verified reads, validation at construction and presigned GETs
> below are carried forward unchanged; the transport, the signing and the
> `objectstore` extra are not. The revisit trigger at the foot of this page is
> what fired. Note that the claim of "adapter contract tests against a stubbed
> transport" was not true of the checkout: those tests were written with
> ADR-0023.
>
> [ADR-0022](0022-single-vm-filesystem-storage.md) amends the production storage
> policy here and remains in force.

## Context

`Settings.object_store_kind` offered `filesystem | s3`, and `production_blockers()`
refused the filesystem store in production — but the container always constructed
`FilesystemObjectStore`. A production deployment could therefore be configured with
`s3`, pass every check, and still be writing evidence to a local directory on the
database's disk. A setting that has no implementation behind it is worse than a
missing feature: it reads as a decision that has been made.

The `objectstore` extra declared `boto3`. Adding it would have pulled a large sync
SDK into an async application for six HTTP verbs.

## Decision

Implement `S3ObjectStore` over `httpx` with SigV4 signed by the standard library's
`hmac` and `hashlib`, and wire `object_store_kind` in the composition root.

- **Two key spaces.** `sha256/ab/cd/<digest>` holds immutable content;
  `records/<object-id>.json` holds one metadata record per (workspace, digest).
  Content is never rewritten; a tombstone updates the record beside it, so
  recording a tombstone never touches the bytes a citation resolves to.
- **Digest verified on read.** A substituted or truncated object raises rather than
  being handed to a parser or a reader.
- **Configuration validated at construction.** A missing bucket, endpoint, region,
  or credential refuses the container, so the failure lands on the deploy rather
  than on the first acquisition run hours later.
- **Presigned GETs** for asset download, so licensed bytes do not stream through the
  API process.
- **Readiness uses `ping()`** — a zero-key list — rather than writing a readiness
  object into the bucket every few seconds.
- `boto3` is dropped from the extras.

## Consequences

- Any S3-compatible endpoint works: AWS, MinIO, Ceph, or an institutional gateway.
  The lab is not obliged to use one vendor to have a durable data plane.
- The signing code is ours to maintain. It is about eighty lines, exercised by
  adapter contract tests against a stubbed transport, and the failure mode is a
  refused request rather than a silent mis-store.
- Two objects exist per stored payload (content plus record). That is the cost of
  keeping filesystem and S3 behaviour identical without a database round trip.

## Alternatives considered

**boto3 in a thread executor.** Works, and is what most projects do. Rejected for
the dependency weight and the sync-in-async seam, not for correctness — this ADR is
the place to revisit if the signing code ever needs a feature SigV4 makes awkward.

**Filesystem only, with a network mount.** An NFS mount behind a content-addressed
store is a durable-looking data plane whose failure modes are the mount's. It also
makes the "not a durable data plane" production blocker unenforceable.

## Validation / revisit trigger

Revisit if a deployment needs multipart upload (objects above ~5 GB), server-side
encryption with customer keys, or a storage class transition — each of those is
where hand-rolled signing stops being the cheap option.

## References

- Target architecture §4.2 (object key convention)
- `packages/python/lab_assistant/adapters/object_store/s3.py`
- Remaining-tasks audit MIT-008
