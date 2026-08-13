Status: active
Owner: product owner
Last verified: 2026-08-13
Source of truth: `schemas/openapi.json`, `services/api/src/lab_assistant_api/routes.py`

# Predecessor API: what maps, what is retired

The successor API is a deliberate break (ADR-0020). This page is the map, and it is
part of the cutover checklist: every predecessor endpoint below is mapped, retired,
or covered by the dual-run procedure. "It changed" is not an entry.

Rows marked **dual run** keep working in the predecessor, read-only, until cutover.

## Authentication

| Predecessor | Successor | Notes |
|---|---|---|
| `POST /api/v1/auth/login` | `POST /api/v1/auth/login` | same shape; the response now lists the caller's workspaces |
| `POST /api/v1/auth/logout` | `POST /api/v1/auth/logout` | unchanged |
| `GET /api/v1/auth/me` | `GET /api/v1/auth/me` | adds role and capabilities |

Tokens are not interchangeable: different signing key, different claims. Everyone
signs in again once at cutover.

## Chat

| Predecessor | Successor | Notes |
|---|---|---|
| `POST /api/v1/chat/sessions` | `POST /api/v1/ask/sessions` | a session may pin a release |
| `GET /api/v1/chat/sessions` | `GET /api/v1/ask/sessions` | |
| `GET /api/v1/chat/sessions/{id}` | `GET /api/v1/ask/sessions/{id}` | messages carry citations and any unsupported handles |
| `POST /api/v1/chat/sessions/{id}/messages` | `POST /api/v1/ask/sessions/{id}/messages` | still SSE; `sources` arrive before tokens; every citation is an opaque handle |
| — | `GET /api/v1/ask/messages/{id}/citations/{handle}` | new: opens the exact evidence |

**Saved chat history is not migrated.** A predecessor answer cites a chunk of a row
that may have changed since; imported here its citations would not resolve, which is
the defect the successor exists to remove. The predecessor stays readable through
cutover — **dual run**.

## Search

| Predecessor | Successor | Notes |
|---|---|---|
| `POST /api/v1/search` | `POST /api/v1/search` | results are release-scoped evidence blocks with handles, not document chunks; `withheld_by_policy` is a steward-only number and `results_filtered` tells every caller their view is filtered |

Scripts that read `results[].document_id` must read `results[].work_id`, and scripts
that expected a `chunk_id` to be stable across time should use the handle, which is
stable within a release by construction.

## Analytics

| Predecessor | Successor | Notes |
|---|---|---|
| `GET /api/v1/analytics/temporal` | `GET /api/v1/analytics/temporal` | release-scoped and policy-filtered |
| `GET /api/v1/analytics/journals` | `GET /api/v1/analytics/journals` | same |
| `GET /api/v1/analytics/mesh_terms` | `GET /api/v1/analytics/subject-terms` | **retired name.** MeSH is one vocabulary asserted by one source; the successor reports subject assertions with their source. A redirect would return different numbers under the old name |
| `GET /api/v1/analytics/topics` | — | **retired.** It reported a clustering nobody could trace to evidence |
| `GET /api/v1/analytics/corpus_stats` | `GET /api/v1/analytics/corpus` | counts follow the retrieval filter; `withheld_by_policy` is `null` for non-stewards |
| — | `GET /api/v1/analytics/sources` | new: per-connector contribution, including unique contribution |

## Feedback

| Predecessor | Successor | Notes |
|---|---|---|
| `POST /api/v1/feedback` | `POST /api/v1/feedback` | adds `flagged_handles`, so a reader can flag a specific citation rather than the whole answer |

## Ingestion and operations

The predecessor's pipeline was scripts (`pipelines/…`) rather than endpoints. Their
successors are API routes and stages: collections and scope versions, discovery runs,
identity decisions, rights decisions, acquisition (including assisted upload),
parsing, extraction, review, releases, and export. There is nothing to redirect;
`docs/product/collections-and-releases.md` is the operational replacement, and
`lab` is the command-line surface.

## Evaluation scripts

Rewritten, deliberately. The predecessor measured recall over **works**; the
successor measures it over **spans**, because retrieving the right paper and the
wrong paragraph is a failure the answer inherits. Benchmarks name works by seed key
(`benchmarks/README.md`), and a case naming an unmapped key fails the run.

## Cutover checklist

1. every row above is either mapped in a caller's code or explicitly retired;
2. members are told that saved chats stay in the predecessor, read-only;
3. notebooks hitting `mesh_terms` or `topics` are updated or retired with their owner;
4. evaluation scripts run against the successor and their numbers are recorded as the
   new baseline;
5. the predecessor goes read-only, and its URL redirects to the successor's login.
