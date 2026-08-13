# Security

## Reporting

Email the security owner privately: `security@example.ac.uk`. Do not open a public
issue, and **never paste a credential, token, or document excerpt into a report**.
Describe what you found and how to reach it; we will ask for what else we need.

Expect an acknowledgement within two working days and an assessment within five.

## What this system holds

- internal user accounts, workspace memberships, and group membership;
- immutable source responses from scholarly APIs;
- acquired publication bytes, whose licences vary and whose rights decisions are
  recorded per action;
- parsed renditions, evidence spans, and reviewed scientific claims;
- chat sessions, answers, and the retrieval provenance behind them.

Full inventory and classification: [`docs/security/data-inventory.md`](docs/security/data-inventory.md).

## Controls that are load-bearing

| Concern | Control |
|---|---|
| Cross-workspace leakage | `workspace_id` required by every repository method; policy filter before *and* after ranking; a two-workspace negative suite that actively attempts inference |
| Stolen session | short-lived tokens, `httpOnly` same-origin cookie, `auth_epoch` revocation, login audit |
| Secret in source | secret scanning in pre-commit and over full history in CI; no runtime secret files; config validation rejects a secret value in a config payload |
| Malicious document | quarantine, malware scan for uploads, resource-limited parser worker with **no network**, no macro execution, formula-injection escaping on export |
| SSRF via an acquisition URL | https only, host allowlist, redirects not followed across hosts, size and duration caps |
| Prompt injection in a source | evidence labelled untrusted to the model, no tools during synthesis, citation handles validated against what the server supplied |
| Unauthorised model transfer | policy evaluated at task creation and again at the provider call; restricted content falls back to local processing or is not processed |
| Tampered asset or export | SHA-256 content addressing, package manifests, checksum validation on use |

## Reporting a suspected leak of another workspace's data

Treat it as a priority incident. Follow
[`docs/operations/runbooks/suspected-access-breach.md`](docs/operations/runbooks/suspected-access-breach.md):
capture evidence first, then revoke, then investigate. Do not "just check" by
querying across the boundary — that adds an event to the audit trail that looks
exactly like the thing being investigated.

## Predecessor incident

The predecessor repository tracked a file named `backend/adminPass`. Any credential
it contained must be treated as exposed: rotate or revoke it, review authentication
logs from the earliest commit that contained it, and record closure. This
repository never imports that file, `.gitignore` refuses it, and pre-commit blocks
a staged file whose name looks like a runtime secret. Details:
[`docs/migration/security-incident.md`](docs/migration/security-incident.md).
