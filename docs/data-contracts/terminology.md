Status: active
Owner: technical owner + data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/contracts/`, `domain/`

# Terminology

Every term below has exactly one meaning in this codebase, in the API, and in the
interface. Ambiguity here is what produced the predecessor's overloaded `status`
column and its conflation of "we never looked" with "nothing is there".

| Term | Means |
|---|---|
| **workspace** | the tenancy and policy boundary. Every row belongs to one; every repository method takes its id. |
| **collection** | a living scope: queries, sources, rules, thresholds. Versioned. |
| **scope version** | one immutable version of a collection's configuration, with a config checksum. |
| **source** | an external system that asserts things: a registry, repository, or index. |
| **source observation** | one immutable record of what a source said, at a time, with the raw response stored. |
| **work** | the abstract scholarly item — a study, a paper, a dataset. Identity decisions attach here. |
| **manifestation** | one concrete published form of a work: version of record, accepted manuscript, preprint. |
| **asset** | bytes, addressed by digest, with a media type and a rights verdict. |
| **rendition** | the structured result of parsing an asset: a tree of addressable nodes. |
| **node** | one addressable element of a rendition — section, paragraph, table, cell, caption. |
| **evidence span** | a stable, quotable reference into a rendition: node id, offsets, quote, normalisation version. |
| **claim** | a proposed or reviewed statement that a work reported a value, bound to an evidence span. |
| **revision** | one version of a claim. Corrections add revisions; they never overwrite. |
| **entity** | a strain, construct, organism, substrate, or compound referenced by claims. |
| **experiment** | the conditions a claim was measured under. |
| **corpus release** | an immutable snapshot: which works, which claims, which spans, which config. |
| **projection** | a serving artefact built from a release: lexical index, embeddings, claim table. |
| **alias** | the name readers use (`current`), pointing at one release. Repointing is publication or rollback. |
| **citation handle** | an opaque server-minted token identifying an evidence span within a release. |
| **task** | one durable unit of work in the database DAG, with a stage, lease, and fingerprint. |
| **stage** | discovery, acquisition, parsing, extraction, indexing, evaluation, reconciliation, export. |
| **grant** | a policy record permitting a subject an action on a resource. |
| **rights verdict** | what may be done with an asset's content, and on what evidence. |
| **ingestion** | **only** the promotion of an approved release into serving indexes. Nothing else. |

## Words deliberately not used

**"Document"** as a system-wide noun. It hid the difference between the abstract work,
the file, and the parsed text — three things with different identity, different rights,
and different lifecycles.

**"Sync"** for discovery. Nothing is synchronised: sources are asked, and their answers
are recorded as assertions that may disagree.

**"Index"** as a verb for the whole pipeline. Indexing is one stage, at the end.
