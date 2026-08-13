Status: accepted
Owner: product owner + technical owner
Last verified: 2026-08-12
Applies to: parsers and the dependency set

# ADR-0011: Parsing stack, project licence, and permissive dependencies

## Context

The whole evidence model rests on the parsing layer, and PDF plus table extraction is
the hardest engineering in the design. The strongest PDF candidate, PyMuPDF, is
dual-licensed AGPL-3.0 or commercial. Adopting it under AGPL has consequences for a
network-served application the day the code is published — a realistic future for a
university research tool. The plan left the project's own licence as an open question,
which meant the parser choice was blocked on it.

## Decision

**The project is MIT, and the dependency set is permissive.** The parsing stack
follows from that:

| Content | Parser | Licence |
|---|---|---|
| JATS/NLM XML | `xml.etree` with namespace-aware traversal | standard library |
| Born-digital PDF | `pypdf` | BSD |
| Scanned PDF | pluggable OCR engine, none by default | — |
| DOCX | `python-docx` | MIT |
| XLSX/ODS | `openpyxl` | MIT |
| CSV/TSV | `csv` with dialect sniffing | standard library |
| HTML | sanitising `html.parser` reader | standard library |

`lxml` and PyMuPDF are banned by a ruff rule and by a dependency test, so the decision
cannot erode quietly. CI fails on an AGPL or GPL-3 dependency.

## Honest limits, recorded rather than hidden

- **Table structure is not extracted from PDF** by a text-only reader. A table-heavy
  PDF is reported as degraded with a warning so a reviewer sees it; the alternative
  is half a table presented as data.
- **A scanned page with no OCR engine yields no text**, and the profile's page
  coverage threshold makes that a degraded rendition rather than an empty success.
- The achievable thresholds live in the parser profiles, set from what these parsers
  actually deliver. Where a plan threshold was not achievable, the *threshold* moved,
  in writing, with the reason.

## Consequences

- The code can be published without a licence conversation.
- Structured XML is materially better than PDF for extraction, which the acquisition
  route order already prefers.
- If table fidelity from PDF becomes essential, that is a new ADR with a licence
  decision attached — GROBID as a separate service is the likely candidate, since it
  is a deployment unit rather than a linked library.

## Alternatives considered

- **PyMuPDF under AGPL.** Rejected while the licence question was open, and now
  rejected by the MIT decision.
- **GROBID now.** Deferred: it adds a JVM deployment unit before any measurement said
  table fidelity was blocking.

## Validation / revisit trigger

`tests/contract/test_module_dependencies.py::test_parsing_layer_avoids_copyleft_pdf_dependency`
and the licence scan in CI. Revisit if a scientific requirement needs PDF table
structure.

## References

- Technical spikes, S1
- `packages/python/lab_assistant/adapters/parsers/`
