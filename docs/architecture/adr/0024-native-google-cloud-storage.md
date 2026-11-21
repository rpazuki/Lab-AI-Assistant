Status: accepted
Owner: technical owner + operator
Last verified: 2026-11-18
Applies to: the data plane

# ADR-0024: A native Google Cloud Storage adapter

Extends [ADR-0023](0023-object-storage-sdk.md) rather than superseding it. The
key convention, the two key spaces, digest-verified reads, validation at
construction and signed GETs are carried forward unchanged. S3 remains
supported exactly as it is; this adds a second provider beside it.

## Context

The shared-archive plan asks for a native GCS adapter so an institution whose
supported object storage is Google's is not forced through an S3
interoperability layer. The alternative — pointing the existing S3 adapter at
GCS's XML interoperability endpoint — was considered and rejected: that endpoint
supports neither GCS generation preconditions nor its resumable uploads, so the
conditional-create and compare-and-swap behaviour ADR-0023 made load-bearing
would silently degrade to check-then-put. An adapter that looks like it has
preconditions and does not is worse than one that says it has none.

The objection to writing this at all was that it could not be tested: there was
no GCS credential and the earlier session could not pull an emulator. That has
changed. `fsouza/fake-gcs-server` runs locally, so the adapter ships with the
same two layers of test the S3 adapter has — stubbed contract tests that need no
network, and an opt-in live-endpoint suite — rather than as code nobody has run.

## Decision

Add `lab_assistant.adapters.object_store.gcs.GcsObjectStore`, selected by
`LAB_OBJECT_STORE_KIND=gcs`, over the maintained `google-cloud-storage` SDK
(Apache-2.0).

It implements the same `ObjectStore` protocol with the same semantics, and the
differences below are provider facts rather than choices.

**Preconditions are generations, not ETags.** A create carries
`if_generation_match=0`, which GCS defines as "only if this object does not
exist". A record update carries `if_generation_match=<generation read>`, which
is the compare-and-swap. These are first-class on GCS and need no feature
detection, so unlike S3 there is no `ConditionalWritesUnsupported` path: every
endpoint that is GCS has them.

**The provider verifies CRC32C and MD5, not SHA-256.** ADR-0023 has the S3
adapter hand the provider a SHA-256 so a damaged upload is rejected at the door.
GCS has no SHA-256 checksum field. The adapter therefore sends an MD5 so the
provider still rejects a corrupted transfer, and **the application's SHA-256
remains the identity check on every read**, exactly as on S3 and the filesystem.
MD5 here is a transport integrity check against accidental corruption and is
used for nothing else; the content identity is the SHA-256 in the key and in the
database.

**Signing may require an explicit key.** A V4 signed URL needs either a service
account's private key or the IAM `signBlob` permission on the running identity.
Where neither is available — a bare instance identity without `signBlob`, or an
emulator — `signed_url` raises rather than returning a URL that will be refused
when somebody clicks it. The caller already treats a failure to sign as a reason
to serve the bytes through the API instead.

## Consequences

Two providers now exist, and only tested providers are supported. The matrix
is: filesystem (single VM, ADR-0022), S3 (ADR-0023) and GCS (this). Anything
else is refused at construction rather than quietly read as a filesystem path.

`google-cloud-storage` is a new dependency, and it is optional: it is imported
inside the adapter, so a deployment on filesystem or S3 neither installs nor
loads it. The same is already true of `boto3`.

*Amended 2026-11-21.* Optional in the package, not in the images: no image installed
either SDK, so no deployment could actually switch to S3 or GCS. The package now has a
`gcs` extra beside `objectstore`, every image installs both, and a missing SDK is a
startup error naming the extra. The adapters still import the SDK only when used.

The emulator proves the semantics this adapter depends on — generation
preconditions, missing-versus-denied, resumable upload, digest round trip. It
does not prove IAM behaviour, signed-URL acceptance by Google, or how a real
bucket's retention and lifecycle rules interact with a tombstone. Those need a
real project, and the live suite skips what the endpoint cannot demonstrate
rather than asserting it passed — the same rule the S3 suite follows.

## Alternatives considered

**The S3 interoperability endpoint.** Rejected above: no generation
preconditions, no resumable uploads, and the degradation is silent.

**One abstract adapter with provider branches.** Rejected. The two providers
differ in their precondition model, their checksum fields and their signing, and
a single class with three `if provider ==` ladders is where those differences go
to be forgotten. Two adapters against one protocol, with one shared contract
test suite that runs both, keeps the differences visible and keeps the
*behaviour* identical where it must be.
