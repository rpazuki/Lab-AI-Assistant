Status: active
Owner: product owner + technical owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/migration/legacy_import.py`, ADR-0015

# Migration overview

This repository is the successor to `RLALab-AI-Assistant`. It is a **clean-slate
implementation with a deliberate port** of the subsystems that carried no
PubMed-shaped assumption — not a rewrite of the old code and not a fork.
The reasoning and the milestone order are in
[ADR-0015](../architecture/adr/0015-delivery-profile.md).

## What changed, and why

| Predecessor | Here | Because |
|---|---|---|
| `documents` + `document_chunks`, one overloaded `status` | works, manifestations, assets, renditions, spans, claims; eight orthogonal states | "not acquired" and "acquired but unparsed" were indistinguishable |
| chunks cited directly | immutable spans inside immutable releases | a citation resolved to a row that could have changed since |
| a live index answered questions | releases behind an alias | the same question gave different answers as material arrived |
| licence as a string | rights decisions per action, with evidence | storing is not displaying is not sending to a model |
| extraction keyed on text hash + `version: 1` | a fingerprint over every declared input | two template revisions shared a cache entry |
| retrieval filtered after ranking | policy before scoring, and again after | a hidden result still influenced the ranking |
| domain schema compiled into the extractor | templates as versioned workspace data | changing a target needed a code release |

## Milestones

- **M1 — "Ask, with real citations."** Complete: workspaces and auth, object store, task
  DAG, connectors, reconciliation with reviewed conflicts, rights decisions, acquisition
  including the assisted loop, JATS and PDF parsing with verified spans, releases,
  lexical and vector projections, release-aware retrieval, answers with resolvable
  citations.
- **M2 — "Datasheets, with evidence."** The claim graph, templates, extraction, review,
  and long-form export are implemented and tested here; the remaining work is real
  templates, real reviewer throughput, and real benchmarks.
- **M3 — "Full replacement."** Refresh, retraction propagation, release comparison, a
  second-domain proof, and cutover.

## Importing the predecessor's data

```bash
lab legacy classify legacy-inventory.jsonl
```

`legacy-inventory.jsonl` is one JSON object per predecessor artefact. The companion
command, `lab legacy re-acquisition-list legacy-inventory.jsonl`, reports what must be
fetched again, grouped by host, with a wall-clock estimate at a policy-compliant rate.

Assets are classified A–E by what can be re-derived:

| Class | Meaning | Action |
|---|---|---|
| A | full text present, licence permits retention | import as an asset, re-parse |
| B | abstract only, metadata sound | import as metadata; acquire if a route exists |
| C | full text present, licence unclear | rights decision required before import |
| D | content whose provenance cannot be established | **re-acquire**; do not import |
| E | duplicate or superseded by a stronger identifier | drop, with the reason recorded |

`ImportReport` reconciles exactly: `mapped + skipped == source_rows`. An import whose
numbers do not add up is a failed import, not an import with a caveat.

**Nothing is imported as evidence.** A predecessor chunk has no verified span, so it
cannot back a citation here. Imported material is metadata and a re-acquisition
worklist; the evidence chain is rebuilt from bytes this system acquired and parsed
itself. That is the whole point of the migration, and shortcutting it would import the
exact defect being fixed.

## Dual running

The predecessor stays available, read-only, until cutover. Members compare answers on
the same questions; the comparison is evaluation signal, and a tripwire in ADR-0015
covers "no measurable improvement".

## Remaining implementation tasks

The critical migration audit found production-readiness gaps that must close before
cutover. Track them in
[Remaining implementation tasks](remaining-implementation-tasks.md).

A follow-up verification found that the resolution log overstates the current state:
MIT-010 is only partially implemented, and two documentation claims need correction.
Track those problems in
[Follow-up implementation issues](follow-up-implementation-issues.md).

## Security

The predecessor tracked a credential file. Treat it as exposed and follow
[the security incident record](security-incident.md).
