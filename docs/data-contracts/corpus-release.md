Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/release.py`, `contracts/release.py`

# Corpus release

A release is an immutable snapshot plus everything needed to reproduce it.

## The manifest

| Field | Why it is in there |
|---|---|
| `release_id`, `workspace_id`, `created_at`, `created_by` | provenance |
| `scope_version_id`, `config_checksum` | which collection configuration |
| `policy_version`, `rights_policy_version` | which rules were in force |
| `template_versions` | which extraction templates, at which versions |
| `embedding_profile` | model, revision, dimensions, normalisation, chunker |
| `parser_profiles` | which parsers produced the renditions |
| `items` | one `ReleaseItemRecord` per work |
| `document_count`, `claim_count`, `span_count` | totals |
| `evaluation_report_digest` | which evaluation run gated it |
| `manifest_sha256` | digest of the canonical bytes of everything above |

`canonical_bytes` serialises deterministically, so the same release always yields the
same manifest digest, on any machine.

## Item content digests

Each item carries a `content_digest` over its rendition digest, its included claim
revisions, its span set, and the chunker and embedding profile. This one field is what
makes releases cheap:

```python
diff = compute_release_diff(previous_manifest, next_manifest)
diff.added, diff.changed, diff.removed, diff.unchanged
```

Publishing copies chunk and embedding rows for `unchanged` items and computes only
`added` and `changed`. **Publishing cost tracks the diff, not the corpus** — a tested
acceptance property, not an intention.

## States

```text
draft -> validating -> approved -> publishing -> published -> superseded
                    -> rejected                            -> withdrawn
```

Transitions are a table, not scattered `if` statements. `published` and later are
immutable: the content cannot change, but the *state* may still advance, because
superseding a published release is exactly what publishing the next one does.

## Validation

`publish_blockers` returns **all** blockers: unreviewed claims, unverified spans,
missing rights decisions, absent evaluation report, failed gate thresholds, missing
projections, empty release. Fixing one problem per publish attempt is a slow way to
discover you had six.

## Aliases

An alias is a named pointer with a history. `switch_to` records the previous target;
`rollback` restores it. Readers never name a release id, so recovery is a pointer
change — the previous release's projections were never deleted.
