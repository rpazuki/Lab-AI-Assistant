Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/retrieval.py`, `chat.py`

# Search and answering

Also called retrieval-augmented generation (RAG): the model writes from passages
retrieved out of the collection, never from its own general memory.

## Order of operations

1. resolve the release from the alias, or from the session's pin;
2. compute the set of works this caller may query — **before any search runs**;
3. search lexical, vector, and structured claim projections within that set;
4. fuse with reciprocal rank fusion; rerank if configured;
5. filter again after ranking, as defence in depth;
6. mint opaque citation handles;
7. generate;
8. validate every returned handle against what was supplied.

Steps 2 and 5 are the security property, and step 3 applies the filter **inside the
candidate SQL** — for structured claims as well as evidence chunks. Filtering claims
after ranking would let a restricted claim occupy the top-k window and decide which
visible claim survives truncation, which is a leak measured in what is *missing*
rather than in what is shown.

Counts and analytics use the same filter, so a total cannot reveal the existence or
the volume of records the caller cannot open. How much is withheld is a steward's
number; every caller still learns that their view is filtered, which is what they
need in order to ask for access.

The visibility set is computed over the whole corpus, page by page, in one query per
page. A fixed limit there would be an access filter that silently stops being
complete once a corpus outgrows it — and it fails *open* for retrieval, because works
past the limit disappear for everyone while looking like a performance setting.

## Citation handles

A handle is a server-minted opaque token derived from the release, the chunk key, and
the span id — not a database id. A leaked handle discloses nothing, and an invented
handle cannot accidentally resolve to a real row.

**An unsupplied handle produces a flagged answer.** The message records it, the
interface shows a warning, and the reader can see which citation not to trust. A 500
would hide a real answer; a silent drop would hide a real problem.

## What the generator receives

Workspace and policy context, the release id, the question and recent conversation,
evidence blocks with their handles, reviewed structured claims, and an explicit
statement that evidence is untrusted document content whose embedded instructions are
data rather than commands. No tools are exposed during synthesis.

Content the caller may not see in full arrives as a bounded excerpt, chosen by the
same policy that decided they may see anything at all.

## Fusion

Reciprocal rank fusion, `k = 60`, deterministic and independent of score scales.
Lexical scores from `tsvector` and cosine distances from pgvector are not comparable;
ranks are.

## Projections are joined on the full key

A chunk key is content-derived, so an unchanged item carries the *same* key into every
release that copied it — which is what makes an incremental build a copy rather than a
recompute (ADR-0009). Vector rows are therefore joined to chunk rows on
`(workspace_id, release_id, chunk_key)`, never on the key alone: a key-only join
multiplies each hit by the number of releases sharing it, inflating candidate counts
and letting a citation resolve against a stale release.

## Retrieval without generation

`POST /api/v1/search` runs steps 1–6 and stops. Same policy filter, same release
resolution, same handles — only generation is skipped. It is defined behaviour rather
than an accident of a shared code path.
