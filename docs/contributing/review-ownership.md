Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `CODEOWNERS`, `.github/pull_request_template.md`

# Review ownership

`CODEOWNERS` decides who must approve. This page says **what that reviewer is
responsible for looking at**, because an approval that means "the diff is tidy" is not
a control.

| Path | Owner | The reviewer's actual job |
|---|---|---|
| `packages/python/lab_assistant/contracts/` | technical owner | is this a breaking change to a shared vocabulary? Is it versioned? |
| `.../domain/` | technical owner | is the invariant expressed as a pure function and tested without I/O? |
| `.../policy/`, `.../provenance/` | security owner | can this widen visibility, weaken a fingerprint, or break the audit chain? |
| `.../application/` | technical owner | transaction boundary correct? Follow-on tasks correct? Errors classified deliberately? |
| `.../adapters/connectors/` | connector owner | are capabilities declared **honestly**, including what it cannot do? |
| `.../adapters/models/` | security owner | is the transfer policy evaluated before the call? |
| `database/migrations/` | technical owner + operator | forward-only? Tested on populated data? Does the running release still work? |
| `configs/examples/templates/` | data steward | is the scientific purpose, exclusion set, and evidence standard right? |
| `benchmarks/` | data steward | would this case change a release gate? |
| `infra/`, `scripts/` | operator | is it idempotent? Does it print a secret? |
| `docs/architecture/adr/` | technical owner | is an accepted ADR being edited rather than superseded? |

## Rules

**An accepted ADR is immutable** except for its status and links. A changed decision is a
new ADR that names the one it supersedes. Editing the reasoning of an accepted decision
erases why the current design exists.

**A bug fix arrives with a failing test first.** Especially a policy or isolation bug —
and the test is named after the defect, so nobody deletes it as redundant later.

**A new state, error class, or outcome needs a taxonomy entry and its retry mapping.**
No literal string states.

**Widening visibility needs the security owner**, whatever else the diff does.

## The pull request template

The checklist asks the questions this codebase has been burned by: does a test fake
raise on an unexpected input, does a monkeypatched await return an awaitable, does a new
repository method take `workspace_id`, does a new float reach a fingerprint, and does a
changed handler's implementation version bump so its fingerprints invalidate.

It is short on purpose. A twenty-item checklist gets ticked without reading.
