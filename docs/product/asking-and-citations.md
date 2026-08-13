Status: active
Owner: product owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/chat.py`

# Asking and citations

## What a good question looks like

The corpus is scoped, so specificity pays. "What lipid titre was reported for
engineered *Y. lipolytica* on glycerol?" retrieves better than "tell me about lipids".
Naming a strain, substrate, or measurement is what the retrieval layer can act on.

## What you get back

- **sources first.** They arrive before the prose, so you can see what the answer is
  grounded in while it is still being written.
- **an answer** synthesised only from those sources.
- **citations** you can click. Each opens the exact passage, in its surrounding
  context, with its work, locator, and release.
- **an honest abstention** when the corpus does not contain the answer. `no_evidence`,
  `insufficient_evidence`, `conflicting_evidence`, `policy_blocked`, and
  `low_confidence` are distinct outcomes and are shown as such. "The corpus does not
  answer this" is a correct result, not a failure.

## Answers are pinned to a release

Every answer records the release it was produced from, so it stays reproducible. A
session can be pinned to a release; otherwise it follows the published alias. If a
paper is later retracted, answers that cited it are flagged retrospectively —
including other members' answers, not only your own.

## Flagged citations

If a generated answer cites a handle the server did not supply, the answer is
**flagged rather than discarded**: the message records the unsupported handle and the
interface shows a warning. A citation you can see is wrong is far safer than one
silently removed — and far more useful than a 500 that hides an otherwise good answer.

A citation handle is an opaque server-minted token, not a database id. Sharing one
discloses nothing, and one you make up will not resolve.

## Restricted material

If a work is licensed for metadata only, or the collection restricts display, you get
a bounded excerpt and a clear marker rather than the full passage. The same policy
decided you could see anything at all.

## Search without an answer

The search surface runs retrieval and stops — same visibility filter, same release,
same handles, no generation. Use it when you want the evidence and intend to read it
yourself.

## Feedback

Rating an answer is worth doing. Ratings are stored against the message with its
release and retrieval parameters, so a pattern of bad answers becomes a benchmark case
rather than a memory.
