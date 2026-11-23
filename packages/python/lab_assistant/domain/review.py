"""Reviewing a large collection by rule rather than one paper at a time.

Three things live here:

* ``WorkSummary``: the handful of canonical fields a steward filters on (title,
  year, kind, journal, subject terms, which sources found it), kept per work so a
  filter is one indexed query instead of a read of every assertion.
* ``DuplicateCandidate``: one row of the duplicate-review queue. The proposal
  itself stays an immutable identity decision; this row is its review state, so a
  merged or dismissed pair leaves the queue.
* ``CandidateFilter`` and ``MergeRule``: what a bulk decision applies to. Both are
  stored with every decision they produce, so the audit trail says which rule
  decided a paper, not only that a person did.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from lab_assistant.domain.identity import normalise_title
from lab_assistant.domain.states import IdentityState, RelevanceState

__all__ = [
    "CandidateFilter",
    "DuplicateCandidate",
    "DuplicateStatus",
    "MergeRule",
    "WorkSummary",
    "summarise_work",
]


class DuplicateStatus(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    DISMISSED = "dismissed"
    #: One side was merged into the other by another decision.
    SUPERSEDED = "superseded"


class WorkSummary(BaseModel):
    """The fields a filter reads, for one work. Rebuilt whenever its assertions change."""

    work_id: UUID
    workspace_id: UUID
    title: str | None = None
    #: ``normalise_title(title)``: what a title search matches against.
    title_search: str = ""
    year: int | None = None
    journal: str | None = None
    resource_kind: str | None = None
    licence: str | None = None
    first_author: str | None = None
    has_abstract: bool = False
    #: Lower-cased subject terms, each wrapped in newlines ("\nyeast\nlipids\n"), so
    #: "has this term" is a plain substring test on every database.
    subject_terms: str = ""
    #: Connector ids, space-wrapped (" crossref pubmed "), for the same reason.
    sources: str = ""
    source_count: int = 0
    updated_at: datetime | None = None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("name", "value", "label", "title"):
            if isinstance(value.get(key), str):
                return value[key]
        return None
    text = str(value).strip()
    return text or None


def summarise_work(
    work_id: UUID,
    workspace_id: UUID,
    metadata: dict[str, Any],
    sources: Iterable[str],
    *,
    now: datetime | None = None,
) -> WorkSummary:
    """A summary from a work's canonical metadata (``canonical_metadata``)."""
    title = _text(metadata.get("title"))
    year = metadata.get("year")
    authors = metadata.get("authors") or []
    first_author = None
    if isinstance(authors, list) and authors:
        first = authors[0]
        first_author = _text(first.get("last_name") if isinstance(first, dict) else first)
    terms = sorted(
        {
            term.casefold()
            for term in (_text(t) for t in metadata.get("subject_terms") or [])
            if term
        }
    )
    connectors = sorted({source for source in sources if source})
    return WorkSummary(
        work_id=work_id,
        workspace_id=workspace_id,
        title=title,
        title_search=normalise_title(title) if title else "",
        year=year if isinstance(year, int) else None,
        journal=_text(metadata.get("journal")),
        resource_kind=_text(metadata.get("resource_kind")),
        licence=_text(metadata.get("licence")),
        first_author=first_author,
        has_abstract=bool(_text(metadata.get("abstract"))),
        subject_terms=f"\n{chr(10).join(terms)}\n" if terms else "",
        sources=f" {' '.join(connectors)} " if connectors else "",
        source_count=len(connectors),
        updated_at=now,
    )


class DuplicateCandidate(BaseModel):
    """A pair waiting for, or past, duplicate review. ``id`` is the proposal's decision id."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    left_work_id: UUID
    right_work_id: UUID
    similarity: float
    year_gap: int | None = None
    first_author_match: bool | None = None
    status: DuplicateStatus = DuplicateStatus.OPEN
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None


def _words(values: Sequence[str]) -> tuple[str, ...]:
    """Each value normalised as a title is, so "Lipid-production" finds "lipid production"."""
    out: list[str] = []
    for value in values:
        normalised = normalise_title(str(value))
        if normalised and normalised not in out:
            out.append(normalised)
    return tuple(out)


class CandidateFilter(BaseModel):
    """Which candidates a list shows, or a bulk decision applies to.

    Every condition must hold. Within one condition, ``title_words`` must all
    appear, ``exclude_words`` must all be absent, and ``subject_terms`` needs any
    one. Merged works are left out unless ``identity_status`` asks for them.
    """

    model_config = {"frozen": True}

    relevance: RelevanceState | None = None
    identity_status: IdentityState | None = None
    #: Leave out works already at this relevance: what a bulk decision has done.
    relevance_not: RelevanceState | None = None
    title_words: tuple[str, ...] = ()
    exclude_words: tuple[str, ...] = ()
    subject_terms: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    resource_kinds: tuple[str, ...] = ()
    journal: str | None = None
    source: str | None = None
    has_abstract: bool | None = None

    @field_validator("title_words", "exclude_words", mode="before")
    @classmethod
    def _normalise_words(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = value.split(",") if "," in value else [value]
        return _words([str(v) for v in value])

    @field_validator("subject_terms", "resource_kinds", mode="before")
    @classmethod
    def _lowercase(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = value.split(",")
        return tuple(dict.fromkeys(str(v).strip().casefold() for v in value if str(v).strip()))

    @field_validator("journal", "source", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        return str(value).strip().casefold()

    @property
    def needs_summary(self) -> bool:
        """Whether any condition reads the summary rather than the work row."""
        return bool(
            self.title_words
            or self.exclude_words
            or self.subject_terms
            or self.year_from is not None
            or self.year_to is not None
            or self.resource_kinds
            or self.journal
            or self.source
            or self.has_abstract is not None
        )

    def matches(self, work: Any, summary: WorkSummary | None) -> bool:
        """The in-memory form of the same test the SQL adapter writes as a query."""
        if self.identity_status is None:
            if work.merged_into_work_id is not None:
                return False
        elif work.identity_status is not self.identity_status:
            return False
        if self.relevance is not None and work.relevance is not self.relevance:
            return False
        if self.relevance_not is not None and work.relevance is self.relevance_not:
            return False
        if not self.needs_summary:
            return True
        if summary is None:
            return False
        # Substrings, as the SQL adapter's LIKE is: "lipid" also finds "lipids".
        if any(word not in summary.title_search for word in self.title_words):
            return False
        if any(word in summary.title_search for word in self.exclude_words):
            return False
        if self.subject_terms and not any(
            term in summary.subject_terms for term in self.subject_terms
        ):
            return False
        if self.year_from is not None and (summary.year is None or summary.year < self.year_from):
            return False
        if self.year_to is not None and (summary.year is None or summary.year > self.year_to):
            return False
        if (
            self.resource_kinds
            and (summary.resource_kind or "").casefold() not in self.resource_kinds
        ):
            return False
        if self.journal and self.journal not in (summary.journal or "").casefold():
            return False
        if self.source and f" {self.source} " not in summary.sources:
            return False
        return self.has_abstract is None or summary.has_abstract is self.has_abstract


class MergeRule(BaseModel):
    """Which open duplicate pairs a bulk merge takes. Each guard is a reason to skip.

    The defaults are the cautious ones: a title match alone is not enough, the
    year must agree, authors must not disagree, both must be the same kind of
    work, two different DOIs mean two publications, and a title that pairs with
    many others ("Editorial", "Correction") is not evidence of anything.
    """

    model_config = {"frozen": True}

    min_similarity: float = Field(default=1.0, ge=0.5, le=1.0)
    require_same_year: bool = True
    #: Skip a pair whose first authors are both known and differ.
    skip_different_authors: bool = True
    #: Also skip a pair where either first author is unknown.
    require_known_authors: bool = False
    #: Skip a pair whose works hold different DOIs (preprint and article, article
    #: and dataset): those are related publications, not duplicates.
    skip_different_dois: bool = True
    #: Skip a pair where either work is in more open pairs than this.
    max_pairs_per_work: int = Field(default=3, ge=1, le=100)
    #: Skip every pair of a group that would merge more works than this at once.
    max_group_size: int = Field(default=3, ge=2, le=20)
