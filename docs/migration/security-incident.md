Status: active
Owner: security owner
Last verified: 2026-08-12
Source of truth: `.gitignore`, `.pre-commit-config.yaml`

# Predecessor credential exposure

## What

The predecessor repository, `RLALab-AI-Assistant`, tracked a file named
`backend/adminPass` in git. Anything it contained must be treated as **exposed from the
commit that introduced it onwards**, whether or not the repository was ever public.

Git history is not a place secrets can be deleted from. A rewrite does not help: every
clone, fork, CI cache, and backup retains the original objects.

## Required actions

1. **Rotate or revoke** whatever that file held — application admin credentials first,
   then anything reachable with them.
2. **Review authentication logs** from the earliest commit containing the file to the
   present, for use of that credential from an unexpected source.
3. **Check for reuse.** A password in a file is a password someone reused. Rotate the
   others.
4. **Record closure**: date, what was rotated, what the log review found, who signed it
   off. An unclosed incident is indistinguishable from an ignored one.

## What this repository does differently

- the file is **never imported** by the legacy importer, which reads database rows and
  ignores repository files;
- `.gitignore` refuses `*Pass`, `*password*`, `*.pem`, `*.key`, `.env`, and the other
  runtime-secret shapes;
- a pre-commit hook blocks a staged file whose **name** looks like a runtime secret —
  content scanning misses a file whose secret is its name;
- `gitleaks` runs in pre-commit and over **full history** in CI, so a secret cannot
  arrive in a squashed branch;
- secrets come only from the environment. `Settings` never reads a secret file, and
  config validation rejects a secret-shaped key inside a config payload, because that
  is how one ends up committed in a YAML file that looked like configuration;
- `lab config print` prints a digest for every secret, never a value — so the diagnostic
  command an operator reaches for under pressure cannot be the leak.

## The generalisable lesson

The predecessor's mistake was not carelessness; it was that nothing in the toolchain
made it impossible. Every control above is mechanical. A rule that depends on
remembering is a rule that fails on the day someone is in a hurry.
