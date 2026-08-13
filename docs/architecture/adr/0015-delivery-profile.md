Status: accepted
Owner: product owner
Last verified: 2026-08-12
Applies to: sequencing and scope

# ADR-0015: Delivery profile B, and the M1-first milestone order

## Context

The migration roadmap's 32–40 week envelope assumed roughly 4 FTE of engineering plus
fractional specialists. The predecessor repository is a single-maintainer,
agent-assisted codebase: about 40,000 tracked lines of Python across 176 files and
15,000 lines of TypeScript, with agent-directed guidance files. If the assumed team
does not exist, the roadmap is not slow — it is unexecutable, and every phase gate in
it is aspirational.

The roadmap also delivered users nothing until Phase 6 while freezing the legacy
system throughout. Eight months of frozen legacy plus no successor was the largest
product risk in the plan and was not in its risk table.

## Decision

**Profile B, hybrid**: a new repository with a new domain and lifecycle, and a
deliberate *port* — not rewrite — of the subsystems that carry no PubMed-shaped
assumption: authentication and the session pattern, the frontend shell, the provider
abstractions, the resilience and outcome taxonomy, the evaluation harness, and the
operations scripts. Each was re-tested against the new contracts rather than copied.

Milestone order, and users see M1 rather than waiting for M3:

- **M1 — "Ask, with real citations."** Workspaces and auth, object store, task DAG,
  connectors, identity reconciliation with reviewed conflicts, rights decisions,
  acquisition including the assisted loop, JATS and PDF parsing with verified spans, a
  corpus release, lexical and vector projections, release-aware retrieval, and answers
  with resolvable citations. **No claim graph, and therefore no claim review.**
- **M2 — "Datasheets, with evidence."** The claim graph, templates, extraction,
  review, and long-form export.
- **M3 — "Full replacement."** Refresh, retraction propagation, release comparison, a
  second-domain proof, and cutover.

The key observation is that retrieval and citation need the evidence chain — work,
asset, rendition, span, release — but not the claim graph. The claim graph is what
makes the datasheet rigorous; it is not what makes the assistant work.

## Consequences

- M1 is already better than the predecessor on the dimension users notice: every
  citation resolves to a verified span in an immutable asset rather than to a chunk of
  a possibly-stale row. It retires the audit's critical findings on access policy and
  unverified evidence without waiting for the claim graph.
- Real questions from real researchers against the new retrieval path arrive as
  evaluation signal months earlier.
- The claim graph is built and tested here too, so M2 is scope already in hand rather
  than a rewrite.

## Tripwires agreed in advance

Tripping one is not failure; failing to notice one is.

| Tripwire | Response |
|---|---|
| Phase 0 measurements incomplete | do not start the next phase; the non-regression gates are undefined without them |
| Re-acquisition list is a large fraction of the corpus, or partly unobtainable | re-scope the first release; decide whether a metadata-plus-abstract tier is acceptable |
| Evidence-span exactness gate unachievable on real PDFs | renegotiate the gate in writing (ADR-0010) before it silently becomes 99 percent |
| Review throughput cannot cover a release | ship M1 without the claim graph; apply the staged review options openly |
| M1 dual-run shows no measurable improvement for researchers | stop and re-examine: the justification is auditability plus quality, and if neither is visible the premise is wrong |
| Elapsed time exceeds 1.5× the envelope with no cutover date | owner decision: continue, freeze scope at M1, or strangle in place |

## Success measured in user terms

Architectural criteria alone describe a system that is *defensible*, not one that is
*used*. Also measured, at M1 dual-run and again at cutover: whether a researcher gets
a useful, correctly cited answer more often than with the predecessor on a blinded
expert rubric; whether they can tell why a claim is supported without leaving the
answer; whether time from "we need a datasheet on X" to "reviewed datasheet exists"
falls, including human review; weekly active lab members against the legacy baseline;
and whether the specific complaints recorded during Phase 0 recur.

## References

- Delivery model, continuity, and tripwires §2–3, §7–8
