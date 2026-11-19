Status: active
Owner: product owner
Last verified: 2026-11-20
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

## Administration: members, invitations, usage

| Predecessor | Successor | Notes |
|---|---|---|
| `GET /api/v1/admin/users` | `GET /api/v1/members` | workspace members, each with usage (sessions, questions, tokens, last active) and their token limit |
| `GET /api/v1/admin/users/{id}` | `GET /api/v1/members` | one list; the detail the predecessor showed is in each row |
| `PATCH /api/v1/admin/users/{id}` (`role`, `is_active`, `token_limit`) | `PATCH /api/v1/members/{user_id}` (`role`, `status`, `token_limit`) | deactivating is now **suspending a membership**, which also ends the member's sessions; `token_limit: null` returns them to the default. Nobody changes their own role or status, and only an admin grants admin |
| — | `POST /api/v1/members/{user_id}/revoke-sessions` | new: sign someone out everywhere without changing their access |
| `POST /api/v1/admin/invitations` | `POST /api/v1/invitations` | **no email is sent**: the answer carries the token once, and the Members page shows the link to send. Inviting an address again revokes its earlier link |
| — | `GET /api/v1/invitations`, `POST /api/v1/invitations/{id}/revoke` | new |
| invite acceptance (web route) | `POST /api/v1/invitations/preview`, `POST /api/v1/invitations/accept` | the token travels in a POST body and in the link's `#fragment`, never in a URL a server logs. An existing account accepts with its own password |
| `GET /api/v1/admin/users/{id}/chat/sessions[/{sid}]` | `GET /api/v1/members/{user_id}/sessions[/{sid}]` | kept as a stated policy (`LAB_CHAT_ADMIN_ACCESS`, on by default): members are told on the Ask page, and every list and read is a `member_chat_read` audit event |
| `GET /api/v1/admin/stats` | Members page tiles, `GET /api/v1/analytics/corpus`, `GET /api/v1/operations/tasks` | the dashboard is split by what it measures |
| account-deactivated page | sign-in refusal | a suspended member's sign-in is a `403` that says the access is suspended |

## Ingestion and operations

The predecessor's pipeline was scripts (`pipelines/…`) rather than endpoints. Their
successors are API routes and stages: collections and scope versions, discovery runs,
identity decisions, rights decisions, acquisition (including assisted upload),
parsing, extraction, review, releases, and export. There is nothing to redirect;
`docs/product/collections-and-releases.md` is the operational replacement, and
`lab` is the command-line surface.

| Predecessor | Successor | Notes |
|---|---|---|
| `/api/v1/admin/ingestion/configs…`, `/ingestion/defaults` | `POST/GET /api/v1/scopes`, `/scopes/{id}/gate`, `/scopes/{id}/approve` | a scope version is immutable; editing creates the next version |
| `/api/v1/admin/ingestion/jobs…`, `/ingestion/worker` | `POST/GET /api/v1/discovery-runs`, `/discovery-runs/{id}/freeze`, `GET /api/v1/operations/tasks` | a job is a discovery run plus its tasks; worker state is read from task rows |
| `/ingestion/jobs/{id}/cancel` | `lab task cancel --task …` | per task; the API offers retry of dead letters (`POST /operations/tasks/{id}/retry`), not cancellation |
| `/ingestion/acquisition-queue[/review-csv]` | `GET /api/v1/acquisition-plans` | the CSV is retired; the queue is a page with an upload per plan |
| `/ingestion/pdf-upload-folders`, `/ingestion/uploads` | `POST /api/v1/bulk-imports`, `lab acquisition import-folder` | PDFs are matched to waiting plans by DOI, PMID or PMCID in the name or on the first page, and go through the assisted-upload checks |
| datasheet manifest / rows / CSV downloads | `POST /api/v1/exports/{release_id}`, `GET /api/v1/exports/{release_id}/download[?file=…]` | the wide-form XLSX is a file of the release's export package |
| `/admin/datasheets/templates…`, `/templates/seed-default` | `GET/POST /api/v1/extraction/templates`, `POST /extraction/templates/{id}/{version}/approve`, `lab template register` | a template is a registered, immutable version; approving one makes extraction use it |
| `/admin/datasheets/runs` (create), `/runs/{id}/reextract`, `/runs/{id}/cancel` | `POST /api/v1/extraction/runs` (`rerun`), `GET /extraction/runs`, `POST /extraction/runs/{id}/cancel` | a run is a batch of extraction tasks over parsed documents |
| `/runs/{id}/extraction-estimate` | `GET /api/v1/extraction/estimate` | sampled; priced at the configured rates |
| `/runs/{id}/rows`, `/runs/{id}/candidates`, `/runs/{id}/datasheet.csv` | `GET /api/v1/extraction/rows`, `GET /extraction/rows.csv?shape=wide|long` | the working sheet of current proposals; the citable sheet is the release export |
| `/runs/{id}/manifest.csv`, `/runs/{id}/acquisition-links.csv` | `GET /api/v1/acquisition-plans` | **retired as files**: the acquisition queue is a page |

## Evaluation

| Predecessor | Successor | Notes |
|---|---|---|
| `/admin/evaluation/question-sets…`, `/questions/{id}`, `/question-sets/{id}/import` | `POST/GET /api/v1/evaluation/benchmarks` | a version is immutable: the Evaluation page's editor opens one and registers the edit as the next version; "Write one" starts a new benchmark |
| `/question-sets/{id}/export` | `GET /api/v1/evaluation/benchmarks/{id}/{version}` | the definition is the export |
| `/admin/evaluation/runs` (create + `/execute`) | `POST /api/v1/evaluation/runs` | queued on request, run by the evaluation worker |
| `/runs/{id}`, `/runs/{id}/results` | `GET /api/v1/evaluation/runs/{id}` | per-case results are in the run |
| `/runs/{id}/cancel` | `POST /api/v1/evaluation/runs/{id}/cancel` | |
| `/admin/evaluation/compare` | `GET /api/v1/evaluation/compare?base=…&candidate=…` | runs of the same benchmark only |
| `/runs/{id}/release-gate`, `/runs/{id}/release-decision` | `PUT/GET /api/v1/evaluation/release-gates`, publish with `override_reason` | a release must pass every required benchmark to publish; an admin may override with an audited reason |
| `/results/{id}/assignments`, `/runs/{id}/review-tasks`, `/reviews…`, `/assignments/{id}/review` | `POST/GET /api/v1/evaluation/runs/{id}/reviews`, `GET /evaluation/reviews/mine`, `PUT /evaluation/reviews/{id}` | same review form; sampled cases of a generation run are shared among reviewers |
| `/runs/{id}/reviews/export`, `/runs/import-report` | — | **retired**: reviews are summarised per run in the API; a report file is not a second source of truth |

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
