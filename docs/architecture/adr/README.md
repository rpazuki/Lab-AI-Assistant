Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: the ADR files in this directory

# Architecture decision records

One ADR per durable decision. An accepted ADR is immutable except for status and
links: a changed decision becomes a **new superseding ADR** so the reasoning that
applied at the time stays readable.

Every ADR has Context, Decision, Consequences, Alternatives considered, and a
revisit trigger. CI checks numbering, status validity, and that a superseded ADR
names its successor.

| ADR | Decision |
|---|---|
| [0001](0001-source-neutral-evidence-graph.md) | Source-neutral evidence graph, not a paper table |
| [0002](0002-modular-monolith-and-workers.md) | Modular monolith with two worker deployments |
| [0003](0003-postgresql-control-plane.md) | PostgreSQL control plane, task queue, and search |
| [0004](0004-content-addressed-objects.md) | Content-addressed immutable object storage |
| [0005](0005-durable-task-dag.md) | Durable PostgreSQL task DAG with leases |
| [0006](0006-tenancy-model.md) | Tenancy-ready, single-tenant by default |
| [0007](0007-identity-reconciliation.md) | Strong-identifier-only automatic merge |
| [0008](0008-claim-and-review-model.md) | Claims graph with review; LLM output is a proposal |
| [0009](0009-releases-and-aliases.md) | Immutable corpus releases behind atomic aliases |
| [0010](0010-evidence-span-normalisation.md) | Versioned evidence-span normalisation contract |
| [0011](0011-parsing-stack-and-licence.md) | Parsing stack, project licence, permissive dependencies |
| [0012](0012-configuration-model.md) | Configuration model carried forward from the predecessor |
| [0013](0013-embedding-profiles.md) | Embedding profiles chosen on measured recall |
| [0014](0014-vector-index-strategy.md) | Vector index type, parameters, and build cost |
| [0015](0015-delivery-profile.md) | Delivery profile and the M1-first milestone order |
| [0016](0016-authentication-and-oidc-path.md) | Password login now, OIDC-ready identity model |
| [0017](0017-object-storage-adapter.md) | S3-compatible object storage, signed without an SDK — superseded by 0023 |
| [0018](0018-connector-rate-budgets.md) | Rate budgets stored, and enforced per request |
| [0019](0019-model-provider-naming.md) | Provider and model naming, and what the defaults mean |
| [0020](0020-predecessor-api-compatibility.md) | The API is a deliberate break, with a written map |

| [0021](0021-worker-network-and-shared-host.md) | Shared-host ports and worker coordinator connectivity |

| [0022](0022-single-vm-filesystem-storage.md) | Optional persistent filesystem storage on one VM |
| [0023](0023-object-storage-sdk.md) | Object storage through the maintained SDK |
| [0024](0024-native-google-cloud-storage.md) | A native Google Cloud Storage adapter |
| [0025](0025-policy-checkpoints.md) | Policy travels as signed checkpoints that narrow and never widen (proposed) |
| [0026](0026-content-replication.md) | Content leaves the producer only under an explicit replication permission (proposed) |
| [0027](0027-publisher-handover.md) | The right to publish moves by a drained, fenced, operator-run handover (proposed) |
| [0028](0028-archive-retention.md) | Archive deletion is reachability-based, planned, reviewed and re-verified (proposed) |
| [0029](0029-reader-import.md) | A reader imports additively, under the producer's ids, and activates by alias (proposed) |
