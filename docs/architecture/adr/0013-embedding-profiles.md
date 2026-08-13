Status: accepted
Owner: technical owner + scientific owner
Last verified: 2026-08-12
Applies to: retrieval quality

# ADR-0013: Embedding profiles chosen on measured recall

## Context

The predecessor used `BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext` as its
primary retrieval encoder. That checkpoint is a masked language model, not a
sentence-embedding model: it was never trained with a contrastive retrieval
objective. Mean- or CLS-pooled raw MLM checkpoints commonly underperform much smaller
purpose-trained bi-encoders on retrieval, sometimes badly, despite better domain
vocabulary. Inheriting it because it is domain-specific is reasoning from vocabulary
rather than from measurement.

## Decision

An embedding **profile** is configuration, and it records everything that makes a
projection reproducible: provider, model, model revision, dimensions, normalisation,
whether producing it sends content externally, index type, and index parameters.
Vectors of different dimensions live in different projection tables and never share a
column.

The model is chosen on **measured recall@k and nDCG@k on the workspace benchmark**,
comparing at minimum: the inherited PubMedBERT configuration as a baseline, a strong
general-purpose retrieval encoder, and a biomedical model trained for retrieval.

The default profile in this repository is `hashing-v1`: a deterministic hashing
embedder. It is not semantic, and it is not a recommendation — it exists so the vector
path is exercised offline and identically on every machine. A deployment sets a
measured profile.

`create_embeddings_external` is a distinct permitted action, so restricted content
needs a local path regardless of what a hosted API would cost.

## Consequences

- Changing the model rebuilds one projection; domain records are untouched.
- The comparison has to be run before the first serious release, which is work the
  plan left implicit.
- Model weights are deployment state cached in a persistent volume, so a container
  rebuild does not re-download them.

## Alternatives considered

- **Keep PubMedBERT because the domain is biomedical.** Rejected as the *default*
  without measurement; it stays in the comparison as the baseline.
- **A hosted embedding API.** Permitted for public content only, and it needs a local
  fallback for restricted material, so it cannot be the only path.

## Validation / revisit trigger

Measured comparison on the workspace benchmark, recorded with the release. Revisit
when the corpus shifts domain or a materially better encoder appears.

## References

- Technical spikes, S3
- `packages/python/lab_assistant/adapters/models/embeddings.py`
