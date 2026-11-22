"""Identity invariants.

The two rules that cannot be configured away: a merge needs a strong identifier
bridge, and a strong-identifier contradiction is a conflict rather than a
last-write-wins update.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.domain.identity import (
    Work,
    WorkIdentifier,
    WorkKind,
    assess_merge,
    normalise_identifier,
    normalise_title,
    propose_duplicates,
    resolve_canonical_field,
    title_similarity,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")


def _work(kind: WorkKind = WorkKind.ARTICLE, workspace: UUID = WORKSPACE) -> Work:
    return Work(
        id=uuid4(),
        workspace_id=workspace,
        work_kind=kind,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _identifier(
    work: Work, scheme: IdentifierScheme, value: str, *, confidence: float | None = 1.0
) -> WorkIdentifier:
    return WorkIdentifier(
        id=uuid4(),
        workspace_id=work.workspace_id,
        work_id=work.id,
        scheme=scheme,
        value_normalized=normalise_identifier(scheme, value),
        value_display=value,
        confidence=confidence,
    )


class TestIdentifierNormalisation:
    @pytest.mark.parametrize(
        ("scheme", "raw", "expected"),
        [
            (IdentifierScheme.DOI, "https://doi.org/10.1234/ABC", "10.1234/abc"),
            (IdentifierScheme.DOI, "doi:10.1234/abc", "10.1234/abc"),
            (IdentifierScheme.PMID, "PMID:0040000001", "40000001"),
            (IdentifierScheme.PMCID, "9000001", "PMC9000001"),
            (IdentifierScheme.PMCID, "pmc:PMC9000001", "PMC9000001"),
            (IdentifierScheme.ARXIV, "arXiv:2401.00001", "2401.00001"),
            (IdentifierScheme.OPENALEX, "https://openalex.org/W123", "W123"),
        ],
    )
    def test_normalises_without_losing_the_original(
        self, scheme: IdentifierScheme, raw: str, expected: str
    ) -> None:
        assert normalise_identifier(scheme, raw) == expected

    def test_blank_identifier_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="normalises to empty"):
            normalise_identifier(IdentifierScheme.DOI, "   ")


class TestMergeInvariants:
    def test_strong_bridge_allows_merge(self) -> None:
        left, right = _work(), _work()
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[_identifier(left, IdentifierScheme.DOI, "10.1/x")],
            right_identifiers=[
                _identifier(right, IdentifierScheme.DOI, "10.1/x"),
                _identifier(right, IdentifierScheme.PMID, "12345"),
            ],
        )
        assert assessment.allowed
        assert assessment.reason_code == "strong_identifier_bridge"
        assert assessment.bridging_identifiers == (("doi", "10.1/x"),)

    def test_identical_titles_alone_never_merge(self) -> None:
        title = "Lipid production in an oleaginous yeast"
        left, right = _work(), _work()
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[
                _identifier(left, IdentifierScheme.TITLE_NORMALISED, title),
                _identifier(left, IdentifierScheme.DOI, "10.1/one"),
            ],
            right_identifiers=[
                _identifier(right, IdentifierScheme.TITLE_NORMALISED, title),
                _identifier(right, IdentifierScheme.DOI, "10.2/two"),
            ],
        )
        assert not assessment.allowed
        # Distinct DOIs on each side: a contradiction, not a match.
        assert assessment.reason_code == "strong_identifier_contradiction"

    def test_title_only_pair_without_strong_ids_is_refused(self) -> None:
        title = "Same title, different papers"
        left, right = _work(), _work()
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[_identifier(left, IdentifierScheme.TITLE_NORMALISED, title)],
            right_identifiers=[_identifier(right, IdentifierScheme.TITLE_NORMALISED, title)],
        )
        assert not assessment.allowed
        assert assessment.reason_code == "weak_evidence_only"

    def test_contradiction_is_reported_with_both_values(self) -> None:
        left, right = _work(), _work()
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[
                _identifier(left, IdentifierScheme.DOI, "10.1/x"),
                _identifier(left, IdentifierScheme.PMID, "111"),
            ],
            right_identifiers=[
                _identifier(right, IdentifierScheme.DOI, "10.1/x"),
                _identifier(right, IdentifierScheme.PMID, "222"),
            ],
        )
        assert not assessment.allowed
        assert assessment.contradictions == (("pmid", "111", "222"),)

    def test_preprint_and_article_are_related_not_merged(self) -> None:
        left = _work(WorkKind.PREPRINT)
        right = _work(WorkKind.ARTICLE)
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[_identifier(left, IdentifierScheme.DOI, "10.1/x")],
            right_identifiers=[_identifier(right, IdentifierScheme.DOI, "10.1/x")],
        )
        assert not assessment.allowed
        assert assessment.reason_code == "incompatible_resource_kind"

    def test_cross_workspace_merge_is_impossible(self) -> None:
        left = _work()
        right = _work(workspace=UUID("00000000-0000-4000-8000-000000000002"))
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[_identifier(left, IdentifierScheme.DOI, "10.1/x")],
            right_identifiers=[_identifier(right, IdentifierScheme.DOI, "10.1/x")],
        )
        assert not assessment.allowed
        assert assessment.reason_code == "cross_workspace"

    def test_low_confidence_blocks_automatic_merge(self) -> None:
        left, right = _work(), _work()
        assessment = assess_merge(
            left=left,
            right=right,
            left_identifiers=[_identifier(left, IdentifierScheme.DOI, "10.1/x", confidence=0.6)],
            right_identifiers=[_identifier(right, IdentifierScheme.DOI, "10.1/x")],
        )
        assert not assessment.allowed
        assert assessment.reason_code == "below_confidence_threshold"

    def test_every_refusal_explains_itself(self) -> None:
        left, right = _work(), _work()
        assessment = assess_merge(left=left, right=right, left_identifiers=[], right_identifiers=[])
        assert assessment.explanation
        assert assessment.as_decision_payload()["reason_code"] == "no_identifier_bridge"


class TestDuplicateProposals:
    def test_proposals_are_review_only(self) -> None:
        left, right = uuid4(), uuid4()
        proposals = propose_duplicates(
            [
                (left, "Lipid production in an oleaginous yeast", 2025, "Ledesma-Amaro"),
                (right, "Lipid production in an oleaginous yeast!", 2025, "ledesma-amaro"),
            ]
        )
        assert len(proposals) == 1
        assert proposals[0].similarity == pytest.approx(1.0)
        assert proposals[0].features["first_author_match"] is True

    def test_distant_years_are_not_proposed(self) -> None:
        proposals = propose_duplicates(
            [
                (uuid4(), "A study of yeast lipids", 2005, None),
                (uuid4(), "A study of yeast lipids", 2025, None),
            ]
        )
        assert proposals == ()

    def test_title_similarity_is_symmetric_and_bounded(self) -> None:
        left = "Lipid production in Yarrowia lipolytica"
        right = "lipid PRODUCTION in yarrowia lipolytica."
        assert title_similarity(left, right) == title_similarity(right, left) == 1.0
        assert title_similarity(left, "") == 0.0
        assert normalise_title("Lipid  production!") == "lipid production"


class TestCanonicalFieldResolution:
    def test_authority_order_decides_and_names_its_sources(self) -> None:
        registry_id, aggregator_id = uuid4(), uuid4()
        value, supporting = resolve_canonical_field(
            [
                {
                    "id": aggregator_id,
                    "value_json": "J Appl Microbiol",
                    "source_priority_class": "aggregator",
                },
                {
                    "id": registry_id,
                    "value_json": "Journal of Applied Microbiology",
                    "source_priority_class": "doi_registry",
                },
            ],
            authority_order=("doi_registry", "aggregator"),
        )
        assert value == "Journal of Applied Microbiology"
        assert supporting == [registry_id]

    def test_empty_input_resolves_to_nothing(self) -> None:
        assert resolve_canonical_field([], authority_order=("doi_registry",)) == (None, [])


class TestDuplicateProposalsAtScale:
    """The fast path finds exactly the pairs comparing every title would."""

    @staticmethod
    def _every_pair(items, threshold=0.85):
        found = []
        for index, (left_id, left_title, left_year, _) in enumerate(items):
            for right_id, right_title, right_year, _ in items[index + 1 :]:
                if left_id == right_id:
                    continue
                if title_similarity(left_title, right_title) < threshold:
                    continue
                if (
                    left_year is not None
                    and right_year is not None
                    and abs(left_year - right_year) > 2
                ):
                    continue
                found.append((left_id, right_id))
        return found

    def test_it_matches_an_exhaustive_comparison(self) -> None:
        import random

        rng = random.Random(7)
        words = [f"w{n}" for n in range(40)]
        items = []
        for _ in range(600):
            title = " ".join(rng.sample(words, rng.randint(1, 12)))
            if items and rng.random() < 0.3:
                # A near copy of an earlier title: one word dropped or added.
                base = rng.choice(items)[1].split()
                if len(base) > 1 and rng.random() < 0.5:
                    base.pop(rng.randrange(len(base)))
                else:
                    base.append(rng.choice(words))
                title = " ".join(base)
            items.append((uuid4(), title, rng.choice([None, 2020, 2021, 2025]), None))
        fast = [(p.left_work_id, p.right_work_id) for p in propose_duplicates(items)]
        assert fast == self._every_pair(items)
        assert fast  # the sample does contain near-duplicates

    def test_a_work_is_never_its_own_duplicate(self) -> None:
        work = uuid4()
        assert propose_duplicates([(work, "Same title", 2024, None)] * 2) == ()
