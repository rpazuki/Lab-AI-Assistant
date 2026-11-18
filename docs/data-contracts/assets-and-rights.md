Status: active
Owner: data steward + admin
Last verified: 2026-11-18
Source of truth: `packages/python/lab_assistant/policy/rights.py`, `application/acquisition.py`

# Assets and rights

## Assets

An asset is bytes plus what is known about them: `sha256` digest, byte size, media
type detected **from magic bytes** rather than an extension or a server's
`Content-Type`, acquisition route, source URL, licence evidence, scan verdict, and
rights verdict.

Storage key: `sha256/<ab>/<cd>/<digest>`. Identical bytes acquired by two routes are
one object with two attempt records. The digest is verified on read, so a corrupted or
substituted object is detected rather than parsed.

**Bytes deduplicate; acquisitions do not.** An asset row is unique per
`(workspace, digest, work, manifestation)`. The same PDF legitimately backs two works —
a dataset paper and its erratum — and each acquisition keeps its own attempt, rights
decision, and audit row while sharing one stored object. Deduplicating on the digest
alone attached the second acquisition to the first work and discarded its rights
decision, which made citations into it point at the wrong paper.

Re-running the *same* acquisition for the same work and manifestation is still a
no-op: that is idempotency, and it is per acquisition rather than per byte string.

## Rights are a decision, not a string

A licence label like `cc-by` does not answer "may we send this to an external model".
`RightsPolicy` evaluates ordered rules against licence, source, publisher, and route,
and returns a verdict per action:

| Action group | Actions |
|---|---|
| metadata | `discover_metadata`, `store_metadata`, `view_metadata`, `export_metadata` |
| content | `store_content`, `process_local`, `view_content`, `display_excerpt`, `display_full_content`, `export_content` |
| model transfer | `send_to_external_model`, `create_embeddings_local`, `create_embeddings_external` |
| serving | `index`, `query` |

Permission to *store* content is not permission to *display* it, and neither is
permission to *send it to a model*. Collapsing those three is how a metadata-only
licence ends up in a provider's request body.

Every verdict carries its evidence: which rule matched, which licence statement, which
policy version, and when it expires. **`unknown` is not `permitted`.** An unknown
verdict blocks content actions and asks a human.

## Rights after publication

A release is immutable and a decision is not: `revoke_rights` rewrites the decision row,
and a snapshot already imported elsewhere cannot hear about it. Current standing therefore
travels separately, as signed policy checkpoints
([ADR-0025](../architecture/adr/0025-policy-checkpoints.md)). A reader applies the
**intersection** of the decision a snapshot froze and the checkpoint's standing, so a
checkpoint can withdraw a permission and never add one; a decision it does not mention is
withheld. Lifting an embargo or renewing a review date is a widening, and reaches readers
with the next snapshot.

## The acquisition ladder

Attempted in order, each attempt recorded with its own outcome:

1. an open-access full-text API (JATS or XML where available);
2. a publisher-declared open-access PDF;
3. a repository or preprint copy, where the manifestation is acceptable;
4. an entitled route the workspace is licensed for;
5. **assisted upload** — a human supplies a copy they are entitled to.

Route 5 is a first-class path, not a failure. The item sits in `awaiting_user`, holding
no lease and counting toward no failure metric.

There is no route that scrapes a page `robots.txt` disallows or bypasses a paywall.
`robots_blocked` and `licence_not_permitted` are permanent outcomes and never retry.

## Scanning

Every acquired or uploaded asset is scanned before it is parsed. Malware quarantines
the asset permanently: no parse, no retry, an audit row, and a steward notification.
The stub scanner is used only when scanning is explicitly disabled, and `server` mode
refuses to start with it enabled.

## Size limits

An asset over the configured limit is `too_large` — permanent, with the actual size
recorded. A worker that OOMs on a 900 MB supplementary file takes its whole queue down
with it.
