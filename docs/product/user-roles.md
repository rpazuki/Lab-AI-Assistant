Status: active
Owner: product owner
Last verified: 2026-11-27
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

Members are invited by an admin; there is no self-registration. On the **Members** page
an admin enters an address and a role and gets a link, shown once, to send however
they like: there is no mailer. The link carries its token after `#`, so no server ever
logs it; it expires (seven days by default), and inviting the same address again
revokes the earlier link. Someone who already has an account accepts with their
existing password.

An admin can tick **Give a temporary password instead of a link**. The invitation's
token is then a password of sixteen characters in four groups (`h7kq-3mzt-x9pe-4wbn`,
with no `0`/`o` or `1`/`l`/`i`), read case-, space- and dash-insensitively. Signed in
with it and its address, `POST /auth/login` answers 401 with
`context.reason = "password_change_required"` and no session; the sign-in page then
asks for a password of the person's own and sends both to `POST /auth/first-sign-in`,
which redeems the invitation and signs them in. It is an invitation in every other
respect: it expires, works once, and is replaced by inviting again or by **New
password** (`POST /invitations/{id}/regenerate`), which keeps the role and the length
of time. An expired or replaced one says so at sign-in (`temporary_password_expired`,
`temporary_password_revoked`), and only to someone who typed both the address and
the password. An address that already has an account is refused one: a password the
admin knows never stands in for one the person chose.

Either way the page drafts the email to send it in, from the admin's own address,
for them to change before sending (`apps/web/src/lib/invitationEmail.ts`).

Suspending a member bumps their `auth_epoch`, which invalidates every issued session
immediately — a revocation that waits for a token to expire is not a revocation — and
refuses their sign-in until an admin reinstates them. "Sign out everywhere" bumps the
epoch without suspending. Nobody changes their own role or status, and only an admin
grants or removes the admin role, so a workspace cannot lose its last admin by
accident.

Each member has a **token allowance**: the tokens their answers have used, in total,
against a limit (the installation default unless an admin sets one). At the limit,
asking is refused until an admin raises it.
