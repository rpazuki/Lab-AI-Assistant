Status: accepted
Owner: security owner + product owner
Last verified: 2026-08-13
Applies to: authentication and sessions

# ADR-0016: Password login now, OIDC-ready identity model

## Context

The predecessor's access model worked and users are used to it: administrator
provisioning or invitation, no self-registration, short-lived bearer tokens held in an
`httpOnly` cookie by the frontend with a same-origin proxy. Imperial provides OIDC via
Azure AD, so institutional login is a realistic next step rather than a hypothetical.

## Decision

Keep the working pattern and make the identity model OIDC-ready:

- provisioning by an administrator or by invitation; no anonymous access;
- Argon2id password hashing, with PBKDF2-SHA256 verifiable for migration and an
  upgrade-on-login path, so adopting a stronger scheme does not force a reset for
  everyone;
- short-lived HS256 tokens carrying issuer, audience, subject, token id, and
  **`auth_epoch`**; bumping the epoch revokes every token already issued to a user,
  which is what a credential incident needs;
- the token lives in an `httpOnly`, `SameSite` cookie set by a Next.js route handler;
  the browser never handles it, so a stolen `localStorage` is not a session;
- external identities are modelled separately and keyed by **issuer + subject**, never
  by email. An identity provider's email is a claim, not a key, and account linking
  needs an authenticated, audited flow.

Logging out bumps the epoch. Clearing a cookie is not a revocation.

## Consequences

- OIDC becomes an added identity source rather than a migration of the user table.
- A password change or an incident invalidates issued tokens immediately, with no
  session store to sweep.
- Login and invitation acceptance are rate-limited and audited, and a failed login
  never reveals which half was wrong. Both limiters are **store-backed** token
  buckets, so they hold across worker processes and survive a deploy — an in-process
  dictionary would be a speed bump, and argon2id makes each guess expensive for the
  server too, so an unthrottled login is also a denial-of-service surface. Two
  buckets: one per account, one per client origin, the second being what makes
  spraying many accounts from one origin expensive. Keys are digests, because rate
  limiting is not a reason to write plaintext emails or addresses into a table
  operators read during incidents. `LAB_LOGIN_RATE_LIMIT_PER_MINUTE` sets the rate; a
  throttled caller gets `429 rate_limited` and still learns nothing about whether the
  account exists.

## Alternatives considered

- **Bearer tokens in `localStorage`.** Rejected: an XSS becomes a session theft, and
  the predecessor had already moved away from it.
- **Server-side session table.** Rejected for now: the epoch gives revocation without
  a store to keep consistent.
- **Wait for OIDC.** Rejected: it blocks every other milestone on an institutional
  timeline.

## Validation / revisit trigger

`services/api/tests/test_api.py::TestAuth` covers revocation, tampering, and the
cookie path. Revisit when the institution's OIDC integration is approved; a forced
password reset at cutover is recommended given the predecessor's credential incident.

## References

- `services/api/src/lab_assistant_api/security.py`
