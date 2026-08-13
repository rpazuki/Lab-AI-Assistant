Status: active
Owner: security owner
Last verified: 2026-08-12
Source of truth: `docs/security/threat-model.md`

# Runbook: suspected access breach

**Symptom.** A member saw something they should not, a session may be stolen, a
credential may be exposed, or data may have crossed a workspace or group boundary.

Priority incident. Order matters: **capture, revoke, investigate, close.**

## 1. Capture

- what was seen, by whom, when, and on which screen or endpoint;
- the `trace_id` if there is one, and the actor and workspace ids;
- a support bundle **now** — logs rotate;
- a database backup, before any remediation.

**Do not reproduce the access to confirm it.** It writes an audit event
indistinguishable from the thing being investigated, and it may widen the exposure.

## 2. Revoke

```bash
lab db revoke-sessions --user <user-id>
```

Bumps `auth_epoch`, invalidating every issued session for that member immediately. Do
this before investigating: a revocation that waits for a token to expire is not a
revocation.

If a credential may be exposed, rotate it — `SECRET_KEY`, database password, provider
key, connector keys — and record which and when. Rotating `SECRET_KEY` invalidates all
sessions, which is the correct outcome here.

## 3. Investigate

- read the audit trail **for the affected workspace only**;
- `lab db verify-audit --workspace <slug>` to confirm the trail itself is intact;
- reconstruct what the actor could reach: role, grants, group memberships, the release
  and alias in force;
- decide whether it was a policy misconfiguration (a grant that should not exist) or a
  defect (policy correct, code wrong). **A defect gets a failing test before a fix**,
  named after the incident, so it cannot regress silently.

## 4. Close

Write it up: what happened, what was exposed, what was revoked and rotated, the root
cause, the test that now covers it, and the date. Tell the affected members and the
data steward — a licence breach may also be a contractual matter, which is theirs to
handle, not the operator's.

## Do not

- **Do not query across the boundary** to see what was exposed.
- **Do not delete the audit rows** of the suspicious access.
- **Do not disable policy checks** to "get people working again". A stack that is up and
  leaking is worse than a stack that is down.
