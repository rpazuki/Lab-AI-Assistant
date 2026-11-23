"""A scope's screening rules: how its concepts and exclusion terms score a paper.

The score is deliberately explainable. Each concept is one line of the scope and
may list alternatives separated by ``|`` ("yarrowia | y. lipolytica"); a concept
is present when any alternative appears in the title, abstract or subject terms.
The score is the share of concepts present, and the scope's relevance policy
turns it into a decision:

* an exclusion term present excludes, whatever the score;
* at or above ``include_threshold`` includes;
* at or below ``exclude_threshold`` excludes, but only when there is an abstract
  to read: a title alone that misses a concept is no evidence the paper does,
  so without an abstract the paper is left for review;
* anything between is left for review, and is never included silently (audit
  finding H-3) unless the policy allows "included, uncertain".

What is read is the work's canonical title and abstract (one each, from the most
authoritative source that gave one) and every subject term any source asserted.
A scope's purpose and intended uses are prose for people and are never matched
as text; exclusion *terms* are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lab_assistant.domain.states import RelevanceState

__all__ = [
    "NO_CONCEPTS",
    "concept_alternatives",
    "has_abstract",
    "plain_text",
    "reason_group",
    "relevance_text",
    "score_for_scope",
    "score_relevance",
]

NO_CONCEPTS = "no_scope_concepts_defined"


def plain_text(value: Any) -> str | None:
    """A field's text, whether a source gave a string or a ``{"name": ...}`` object."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("name", "value", "label", "title"):
            if isinstance(value.get(key), str):
                return value[key].strip() or None
        return None
    text = str(value).strip()
    return text or None


def relevance_text(metadata: Mapping[str, Any]) -> str:
    """What the rules read: title, abstract and subject terms, case-folded.

    ``metadata`` is a work's canonical metadata, as ``canonical_metadata`` builds it.
    """
    parts = [plain_text(metadata.get(name)) for name in ("title", "abstract")]
    parts.extend(plain_text(term) for term in metadata.get("subject_terms") or [])
    return " ".join(part for part in parts if part).casefold()


def has_abstract(metadata: Mapping[str, Any]) -> bool:
    return plain_text(metadata.get("abstract")) is not None


def concept_alternatives(concept: str) -> list[str]:
    """``"yarrowia | y. lipolytica"`` -> ``["yarrowia", "y. lipolytica"]``."""
    return [alt.strip().casefold() for alt in concept.split("|") if alt.strip()]


def score_relevance(
    text: str,
    *,
    concepts: Sequence[str],
    exclusion_terms: Iterable[str],
    include_threshold: float,
    exclude_threshold: float,
    allow_included_uncertain: bool,
    has_abstract: bool,
) -> tuple[RelevanceState, str]:
    """The decision these rules make for one paper, and the reason recorded with it."""
    parsed = [alts for alts in (concept_alternatives(c) for c in concepts) if alts]
    if not parsed:
        return RelevanceState.UNREVIEWED, NO_CONCEPTS
    for term in exclusion_terms:
        needle = term.strip().casefold()
        if needle and needle in text:
            return RelevanceState.EXCLUDED, f"exclusion_term:{term.strip()}"
    hits = sum(1 for alts in parsed if any(alt in text for alt in alts))
    score = hits / len(parsed)
    if score >= include_threshold:
        return RelevanceState.INCLUDED, f"concept_score:{score:.2f}"
    if score <= exclude_threshold:
        if not has_abstract:
            return RelevanceState.UNREVIEWED, f"no_abstract_concept_score:{score:.2f}"
        return RelevanceState.EXCLUDED, f"concept_score:{score:.2f}"
    if allow_included_uncertain:
        return RelevanceState.INCLUDED_UNCERTAIN, f"concept_score:{score:.2f}"
    return RelevanceState.UNREVIEWED, f"uncertain_concept_score:{score:.2f}"


def score_for_scope(text: str, scope: Any, *, has_abstract: bool) -> tuple[RelevanceState, str]:
    """``score_relevance`` with a scope version's concepts, terms and policy."""
    policy = scope.relevance_policy
    return score_relevance(
        text,
        concepts=tuple(scope.concepts or ()),
        exclusion_terms=tuple(getattr(scope, "exclusion_terms", ()) or ()),
        include_threshold=policy.include_threshold,
        exclude_threshold=policy.exclude_threshold,
        allow_included_uncertain=policy.allow_included_uncertain,
        has_abstract=has_abstract,
    )


def reason_group(reason: str) -> str:
    """A reason without its number, for counting: ``concept_score:0.50`` -> ``concept_score``.

    An exclusion term keeps its term, so a preview says which term excluded how many.
    """
    if reason.startswith("exclusion_term:"):
        return reason
    return reason.split(":", 1)[0]
