Status: active
Owner: security owner + data steward
Last verified: 2026-11-20
Source of truth: `packages/python/lab_assistant/adapters/db/tables.py`

# Data inventory

| Data | Classification | Store | Retention | Notes |
|---|---|---|---|---|
| Member accounts (email, name, role) | personal, internal | PostgreSQL | while the member is active, then anonymised | argon2id hashes; no plaintext ever |
| Sessions and `auth_epoch` | credential-adjacent | PostgreSQL | short expiry | bumping the epoch revokes immediately |
| Invitations | personal | PostgreSQL | until accepted or expired | single-use tokens, hashed |
| Source observations, raw responses | public metadata, mostly | objects + PostgreSQL | indefinite | provenance; re-parseable |
| Acquired assets | **licence-dependent** | object store | per rights verdict | verdict recorded per action; expiry honoured |
| Renditions and evidence spans | derived, licence-dependent | objects + PostgreSQL | with the asset | immutable |
| Claims and revisions | internal research data | PostgreSQL | indefinite | the lab's own scientific record |
| Chat sessions, questions, answers | personal, internal | PostgreSQL | configurable per workspace | questions can reveal unpublished research direction |
| Feedback ratings | internal | PostgreSQL | indefinite | feeds evaluation |
| Audit events | internal, integrity-critical | PostgreSQL | indefinite, append-only | content redacted on write |
| Structured logs | operational | files, rotated | 20 MB × 5 per service | redacted at emit |
| Export packages | licence-dependent | object store | until deleted by a steward | rights enforced at build |
| Embedding vectors | derived | PostgreSQL (pgvector) | with the release | derived from licensed text; treated as content |
| Model request envelopes | licence-dependent | object store | per policy | stored for provenance, access-controlled |
| Secrets | credential | environment only | rotate on suspicion | never a file in the repository, never in a log or audit row |

## Rules that follow from the table

**Embeddings are content.** A vector derived from restricted text inherits its
restriction. Treating a projection as "just numbers" is how licensed text leaves a
system.

**Questions are sensitive.** "Has anyone reported a titre above X for this construct"
discloses what the lab is working on. Chat history is per-member: one member asking
for another's session gets a `404`. The one exception is **admin oversight**, carried
over from the predecessor as a stated policy of the installation
(`LAB_CHAT_ADMIN_ACCESS`, on by default): an admin of the workspace may list and read a
member's conversations, deleted ones included. Members are told so on the Ask page,
and every list and every read writes a `member_chat_read` audit event naming the admin
and the member. Turning the setting off refuses those reads with a `403`.

**Usage is metered, not content.** Each answer records the tokens it used. A member's
total is what their allowance (`LAB_CHAT_TOKEN_LIMIT_DEFAULT`, or their own limit) is
measured against; deleting a conversation hides it but does not refund it. Admins see
per-member counts — sessions, questions, tokens, last active — on the Members page.

**Audit rows hold ids, not content.** Prompts, quotes, credentials, and signed URLs are
redacted on the way in. Raw material lives in access-controlled object storage with its
own retention, or nowhere.

**Aggregates are data too.** A chunk total, a candidate count, or a "withheld" figure
measures material the caller may not open. Analytics and search apply the retrieval
filter to their counts, and the *size* of what is withheld is reported only to callers
who may manage the release. Every caller still learns that their view is filtered,
which is what they need in order to ask for access.

**Effective configuration is internal.** `/api/v1/config` redacts secrets to digests
and still requires the operations capability: topology, provider choices, and staged
controls are exactly what someone probing the deployment wants.

**Deletion is a real operation.** Removing a member anonymises their identity and
retains their review decisions, because a published claim's provenance cannot become
anonymous without invalidating the release that depends on it. The claim's reviewer
becomes an internal id with a recorded deletion event.

## Personal-data requests

Export or deletion for one member: their account fields, memberships, chat sessions,
and feedback. Their review decisions are retained and pseudonymised, and the reason is
recorded above. Route these through the data steward, not the operator.
