Status: active
Owner: technical owner + data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/provenance/`

# Provenance

## The chain a citation resolves

```text
message citation -> evidence span -> rendition -> asset -> acquisition attempt
                 -> manifestation -> work -> source assertions
```

That is the minimum defensible provenance for a research answer, and
`LineageResolver` reports **where it breaks** rather than stopping one link short and
looking complete.

## Fingerprints

Every reproducibility claim reduces to a canonical digest. The extraction fingerprint
is the one the predecessor got wrong — its key was selected-text hash, an integer
template version, and a model name, so two different templates at version `1` could
share a cache entry and a prompt change could silently reuse stale output.

The fingerprint now covers: workspace and policy scope, rendition digest, the ordered
selected node ids and their span digests, deterministic metadata, template id *and*
version *and* schema *and* target definitions, prompt bundle id and version, selector,
prompt-builder, validator and parser versions, and provider, model revision, settings,
and output mode.

A cache hit cannot change review state, and cannot cross a workspace or policy
boundary.

## Canonicalisation

One value, one byte string: sorted keys, ordered sequences, NFC text, UTC timestamps.

Floats are **accepted in payloads** — a rendition records bounding boxes and OCR
confidences, and refusing them would mean inventing a parallel encoding for real
measurements — and **refused in fingerprints**, because an accumulated float
(`0.1 + 0.2`) is not the number anyone configured. A caller holding a genuinely
configured float converts it through `stable_number` at the call site that decided it
was authoritative.

## Audit

Append-only rows with a per-workspace hash chain, so a deleted or reordered row is
detectable without trusting ids or timestamps. On PostgreSQL a trigger refuses updates
and deletes outright.

Audit rows say *what happened to which id*. Content, prompts, evidence quotes,
credentials, and signed URLs are redacted on the way in — raw material belongs in
access-controlled object storage with its own retention policy, or nowhere.
