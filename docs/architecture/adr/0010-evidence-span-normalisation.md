Status: accepted
Owner: technical owner + scientific owner
Last verified: 2026-08-12
Applies to: evidence verification
Source of truth: `packages/python/lab_assistant/domain/normalisation.py`

# ADR-0010: Versioned evidence-span normalisation contract

## Context

The plan required that a span's quote "matches the referenced rendition node", and
set evidence-coordinate validity at 100 percent. "Exact" was undefined — and for PDFs
the naive reading is unachievable. Between a PDF's bytes and any quotable string sit
ligature decomposition, soft and hard hyphenation across line and page breaks,
multi-column reading order, flattened superscripts, Unicode normalisation forms,
non-breaking and thin spaces, en dashes against hyphens, Greek letters from symbol
fonts, and per-engine whitespace policy. Two parsers of one PDF do not produce
byte-identical text, and a model quoting supplied evidence will not reproduce a
parser's whitespace.

A gate whose feasibility is unknown is not a gate. It gets renegotiated downward the
first time a real paper fails it.

## Decision

Define a **versioned normalisation contract** and state the gate against it.

Version 1 chain, in order: Unicode NFC; compatibility-duplicate folding (micro sign
to Greek mu, ohm sign to omega); typographic ligature expansion; soft-hyphen removal
then hyphenation rejoining across line breaks; dash folding to hyphen-minus and
quotation folding to ASCII; whitespace unification and collapsing.

Never folded: digits, **letter case**, Greek letters, superscript and subscript
characters. Case matters because `pex10` and `PEX10` are different genes, and a model
that changed case paraphrased rather than quoted.

Raw offsets survive: the index map returns offsets into the original node text, so the
original characters stay addressable.

The gate: **100 percent of accepted claims have spans that resolve to their rendition
node and match under normalisation version N.** That is provable. "Exact bytes" is not.

## Consequences

- A failed verification rejects the proposal as unverified evidence. It is never
  repaired — trimming a quote to make it fit would defeat the mechanism.
- Changing the chain requires a new version; prior verifications record the version
  that produced them and are not silently revalidated.
- Ambiguity is reported: a quote occurring twice in a node is flagged, and a supplied
  offset breaks the tie.

## Alternatives considered

- **Byte equality.** Rejected: unachievable for PDF-derived text, so it would be
  quietly weakened.
- **Fuzzy similarity above a threshold.** Rejected: a threshold is a dial someone
  turns under pressure, and a near-match is not a quote.

## Validation / revisit trigger

`packages/python/tests/test_normalisation.py` is the contract. Revisit when a parser
or format produces a class of legitimate mismatch the chain does not cover — and
publish a new version rather than loosening version 1.

## References

- Technical spikes, S2
