"""The evidence-span normalisation contract (spike S2).

These tests are the contract. If the chain changes, they fail — which is the
point: the version exists so a change is a decision rather than a drift.
"""

from __future__ import annotations

import pytest

from lab_assistant.domain.normalisation import (
    SPAN_NORMALISATION_VERSION,
    normalise_for_comparison,
    normalise_with_map,
    verify_quote,
)


class TestNormalisationChain:
    def test_version_is_recorded(self) -> None:
        assert SPAN_NORMALISATION_VERSION == "1"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ligatures expand
            ("The eﬀect of the ﬁnal ﬂask", "The effect of the final flask"),
            # whitespace of every kind collapses
            ("a b\tc\nd e", "a b c d e"),
            # dashes fold to hyphen-minus
            ("0.4–0.5", "0.4-0.5"),
            ("30−C", "30-C"),
            # quotes fold to ASCII
            ("“yield” and ‘basis’", '"yield" and \'basis\''),
            # soft hyphen disappears
            ("experi­ment", "experiment"),
            # hyphenation across a line break rejoins
            ("experi-\n    ment", "experiment"),
            # micro sign folds onto Greek mu, which units depend on
            ("5 µL", "5 μL"),
        ],
    )
    def test_transformations(self, raw: str, expected: str) -> None:
        assert normalise_for_comparison(raw) == expected

    def test_case_is_never_folded(self) -> None:
        # PEX10 and pex10 are different genes. Case-insensitive matching would
        # accept a paraphrase as a quote.
        assert normalise_for_comparison("PEX10") != normalise_for_comparison("pex10")

    def test_digits_superscripts_and_greek_survive(self) -> None:
        raw = "OD₆₀₀ was 12.5 and α-amylase rose 10²-fold"
        assert normalise_for_comparison(raw) == raw

    def test_hyphen_before_a_capital_is_not_rejoined(self) -> None:
        # "...anti-\nCRISPR" is a real hyphenated compound, not a line break split.
        assert normalise_for_comparison("anti-\nCRISPR") == "anti- CRISPR"

    def test_index_map_points_back_at_raw_characters(self) -> None:
        raw = "The eﬀect  of\nlipid"
        normalised, index_map = normalise_with_map(raw)
        assert normalised == "The effect of lipid"
        assert len(index_map) == len(normalised)
        # Both characters of the expanded ligature map to the one source index.
        ligature_index = raw.index("ﬀ")
        assert index_map[normalised.index("ff")] == ligature_index
        assert index_map[normalised.index("ff") + 1] == ligature_index

    def test_normalisation_is_idempotent(self) -> None:
        raw = "  a  b-\ncd  "
        once = normalise_for_comparison(raw)
        assert normalise_for_comparison(once) == once


class TestQuoteVerification:
    NODE = (
        "Strain YL-A reached a lipid yield of 0.42 g/g substrate at 30 degrees C, "
        "measured in triplicate."
    )

    def test_exact_quote_verifies_with_raw_offsets(self) -> None:
        result = verify_quote(node_text=self.NODE, quote="lipid yield of 0.42 g/g substrate")
        assert result.verified
        assert result.raw_start is not None and result.raw_end is not None
        assert self.NODE[result.raw_start : result.raw_end] == "lipid yield of 0.42 g/g substrate"
        assert result.normalisation_version == SPAN_NORMALISATION_VERSION

    def test_quote_differing_only_in_typography_verifies(self) -> None:
        node = "The yield was 0.42–0.55 g/g substrate"
        result = verify_quote(node_text=node, quote="yield was 0.42-0.55 g/g substrate")
        assert result.verified

    def test_paraphrase_is_rejected_not_repaired(self) -> None:
        result = verify_quote(node_text=self.NODE, quote="the lipid yield was about 0.4")
        assert not result.verified
        assert result.failure_code == "quote_not_in_node"
        assert result.raw_start is None

    def test_blank_quote_is_rejected(self) -> None:
        assert verify_quote(node_text=self.NODE, quote="   ").failure_code == "empty_quote"

    def test_ambiguity_is_reported_and_offsets_break_the_tie(self) -> None:
        node = "yield 0.42 g/g. Later, yield 0.42 g/g again."
        first = verify_quote(node_text=node, quote="yield 0.42 g/g")
        assert first.verified and first.occurrences == 2 and first.is_ambiguous
        second = verify_quote(node_text=node, quote="yield 0.42 g/g", expected_start=23)
        assert second.raw_start is not None and second.raw_start > 10

    def test_verification_survives_re_serialisation(self) -> None:
        # Property the plan asks for: the check is a pure function of the text, so
        # round-tripping the rendition through JSON cannot change the verdict.
        import json

        payload = json.loads(json.dumps({"text": self.NODE}))
        assert (
            verify_quote(node_text=payload["text"], quote="0.42 g/g substrate").verified
            is verify_quote(node_text=self.NODE, quote="0.42 g/g substrate").verified
        )
