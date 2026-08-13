Status: active
Owner: admin + security owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/policy/model_transfer.py`

# Model transfer policy

Sending a passage to an external model provider is a **transfer of licensed content to
a third party**. It gets its own policy, its own actions, and its own audit trail.

## The three actions

| Action | Question it answers |
|---|---|
| `process_local` | may this content be parsed and chunked on our own machines? |
| `create_embeddings_local` / `create_embeddings_external` | may a vector be computed here / there? |
| `send_to_external_model` | may this text be sent to a provider? |

A licence permitting the first two says nothing about the third. Collapsing them is
exactly how metadata-only material ends up in a request body.

## Evaluated twice

Once when the task is created — so an impermissible task is never queued — and again
**immediately before the provider call**, because a rights verdict can expire and a
policy can change between queueing and execution. The second check is the one that
counts; the first exists so the queue does not fill with work that will be refused.

## What is transferred

Only the selected evidence nodes, and only the ones policy permits. Not the whole
document, not neighbouring restricted sections, and never the raw asset. The request
envelope is stored by digest so the exact transferred content is auditable after the
fact.

## Provider properties are declared

Each provider declares data residency, retention, whether inputs may train a model, and
whether a zero-retention path is available. A provider that will not state its retention
is not configurable in `server` mode — the check is in `production_blockers`.

## Fallbacks, in order

1. an approved external provider, if the transfer is permitted;
2. a local model, if one is configured;
3. **abstain**, with `not_searched_policy` — recorded as a policy abstention, never as
   "the source does not report it".

Step 3 is a correct outcome. Silently degrading it into a negative statement about the
source is the failure mode this whole taxonomy exists to prevent.

## Audit

Every transfer records: actor, workspace, work, span ids, action, policy version,
rights verdict and its evidence, provider, model revision, envelope digest, and token
counts. **Not the content** — that is in object storage under access control.

## Changing the policy

Admin only, versioned, and audited. A release records the policy version in force when
it was published, so a later loosening cannot retroactively justify an earlier
transfer.
