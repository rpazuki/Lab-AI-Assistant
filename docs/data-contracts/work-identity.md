Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/identity.py`

# Work identity

The hardest problem in the system: deciding that two observations describe the same
work. Get it wrong by merging, and two studies' numbers pool into one fictional
result. Get it wrong by splitting, and the same paper appears twice with different
claims.

## Identifier classes

| Class | Identifiers | Merge authority |
|---|---|---|
| **strong** | DOI, PMID, PMCID, arXiv id, ISBN, accession | merges automatically |
| **weak** | normalised title, first-author + year, journal + volume + pages | **proposes only** |

`normalise_identifier` lowercases DOIs, strips `doi:` and resolver prefixes, removes
version suffixes where the scheme says they are not identity-bearing, and rejects a
value that does not match the scheme's shape.

## The rule

**Automatic merge on strong identifiers only.** A weak match never merges by itself, at
any similarity threshold. There is no threshold at which title similarity is safe,
because errata, corrections, translations, and conference-then-journal pairs share
titles by design.

```python
assessment = assess_merge(left, right)
assessment.decision  # merge | conflict | propose | distinct
assessment.reasons  # ('doi_match',) — or ('doi_conflict', 'pmid_match')
```

## Conflicts

Two works sharing one strong identifier while contradicting on another produce
`identity_conflict`, not a merge and not a silent choice. The item enters the steward
queue with both assertions and their sources visible.

## Canonical field values

When merged observations disagree about a field, `resolve_canonical_field` picks by
source precedence and records **which source won and which lost**. The losers are kept.
A canonical value with no trail is an unfalsifiable assertion.

## Splitting

A merge is reversible. Splitting restores the constituent works, re-attaches claims to
the work whose rendition their spans belong to, and flags any claim whose attachment is
ambiguous. A merge that cannot be undone would make every identity decision permanent
on first guess.
