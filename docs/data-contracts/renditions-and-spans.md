Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/normalisation.py`, `domain/renditions.py`

# Renditions and spans

## Renditions

Parsing produces a **rendition**: an ordered tree of addressable nodes, with the parser
profile, its version, and quality metrics recorded. A rendition is content-addressed
and immutable; a better parser produces a *new* rendition rather than mutating one that
existing claims cite.

Node kinds: `document`, `section`, `paragraph`, `list`, `list_item`, `table`,
`table_caption`, `table_footnote`, `figure`, `figure_caption`, `reference_list`,
`reference`, `abstract`, `title`, `sheet`, `row`, `cell`, `page`.

Tables are structured — cells with row and column indices and spans — not flattened
text. A table flattened into a paragraph loses exactly the numbers extraction exists to
find.

Each node carries source coordinates: page and bounding box for a PDF, element path for
JATS, sheet and cell reference for a workbook.

## Quality is measured

`ParseQualityMetrics` records character count, the fraction of characters recognised,
sections detected, tables detected, whether any text layer existed at all, and whether
OCR ran. A rendition below the configured threshold is `degraded` and **cannot be
extracted from** until a reviewer accepts it explicitly. Extracting numbers from an
80%-garbled OCR page produces confident nonsense.

## Evidence spans

A span is `(rendition_id, node_id, start_offset, end_offset, quote,
normalisation_version)`.

Offsets are into the node's **normalised** text; the raw offsets are recoverable
through the index map the normaliser returns, so a span can still be highlighted in the
original bytes.

## The normalisation contract, version 1

Verification asks: **does the quote still match under normalisation version 1** — not
does it match byte for byte. Publishers re-issue files with different Unicode
decisions, and an exact-bytes gate fails on all of it and teaches everyone to bypass
the check.

Version 1 applies, in order:

1. NFC normalisation;
2. compatibility-duplicate folding — micro sign to Greek mu, and the other
   canonical-equivalent duplicates;
3. ligature expansion (`ﬁ` → `fi`);
4. soft-hyphen removal, and rejoining of line-break hyphenation;
5. dash folding (en, em, minus, non-breaking hyphen → hyphen-minus) and quote folding
   (curly → straight);
6. whitespace collapse, including non-breaking and zero-width space;
7. control-character removal, except tab and newline.

**Case is never folded.** `Ura3` and `URA3` are different things in this domain.

```python
verify_quote(node_text, span)  # True | False, under SPAN_NORMALISATION_VERSION
```

The version is stored on every span. Changing normalisation means version 2, a
re-verification pass, and spans that fail flagged for review — never a silent
reinterpretation of existing spans.

## Re-anchoring

When an asset is re-parsed, spans are re-anchored to the new rendition by normalised
quote plus node-kind and position hints. Anything that cannot be re-anchored is flagged
for review. Dropping it would delete a reviewed claim's evidence; keeping it silently
would point a citation at the wrong text.
