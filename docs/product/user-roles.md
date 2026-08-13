Status: active
Owner: product owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/workspace.py`

# Roles and capabilities

Five roles. A role is a bundle of capabilities; policy grants are checked separately
and can restrict what a role would otherwise reach.

| Capability | member | reviewer | steward | admin | auditor |
|---|---|---|---|---|---|
| ask questions, search | ✅ | ✅ | ✅ | ✅ | ✅ |
| read reviewed claims | ✅ | ✅ | ✅ | ✅ | ✅ |
| create and edit collections | – | – | ✅ | ✅ | – |
| run discovery and acquisition | – | – | ✅ | ✅ | – |
| upload an assisted asset | ✅ | ✅ | ✅ | ✅ | – |
| review claims | – | ✅ | ✅ | ✅ | – |
| approve and publish a release | – | – | ✅ | ✅ | – |
| roll back an alias | – | – | ✅ | ✅ | – |
| export a package | – | ✅ | ✅ | ✅ | – |
| manage members and grants | – | – | – | ✅ | – |
| read the audit trail | – | – | ✅ | ✅ | ✅ |
| change rights or transfer policy | – | – | – | ✅ | – |

**Auditor is read-only by construction.** It can read the audit trail and verify the
hash chain and cannot mutate anything — so an oversight function needs no capability
that could alter what it is inspecting.

**A steward cannot silently change policy.** Rights and model-transfer policy are
admin-only, versioned, and audited; a release records which policy version was in
force when it was published.

## Two things a role never grants

**Seeing restricted content.** Role is one input; a policy grant is another. A member
of a group-restricted collection needs the grant as well as the role, and a blanket
user grant does not unlock group-restricted material — that combination was a real
bug, and there is a test named after it.

**Bypassing review.** No role can promote an unreviewed claim into a release. There is
no override flag, because a flag like that becomes the normal path within a month.

## Joining and leaving

Members are invited by an admin; there is no self-registration. Deactivating a member
bumps their `auth_epoch`, which invalidates every issued session immediately — a
revocation that waits for a token to expire is not a revocation.
