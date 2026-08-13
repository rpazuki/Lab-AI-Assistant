# Restricted benchmark manifests

Status: active
Owner: data steward
Last verified: 2026-08-12

A benchmark whose content is licensed article text is **not** committed here. This
directory holds manifests that point at it: where it lives, who owns it, what may
be done with it, and the checksum of the copy the last evaluation used.

Manifest shape:

```yaml
benchmark_id: rla-706-doi-gold
version: 1.0.0
kind: discovery
location: institutional-store://benchmarks/rla-706-doi-gold-v1
sha256: <digest of the archive>
owner: data steward
licence: publisher terms, internal evaluation only
permitted_use: internal evaluation; never sent to an external model provider
sensitivity: restricted
notes: >
  Inherited from the predecessor as a migration benchmark. It measures no-loss
  migration in one domain; it is not the universal benchmark for this product.
```

The 706-DOI set from the predecessor belongs here when it is imported: it is a
migration benchmark for one lab's domain, and at least one different lab-domain
fixture is needed before the core can be called lab-agnostic.
