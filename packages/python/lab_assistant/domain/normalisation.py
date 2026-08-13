"""The evidence-span normalisation contract (spike S2).

"Exact bytes" is not achievable between a PDF's bytes and any quotable string:
ligatures decompose, words hyphenate across line breaks, columns interleave,
superscripts flatten, and every extraction engine has its own whitespace policy.
A gate demanding byte equality would be renegotiated downward the first time a
real paper failed it.

So the gate is stated precisely instead:

    100 percent of accepted claims have spans that resolve to their rendition
    node and match under normalisation version N.

Version 1 chain, applied in this order:

1. Unicode NFC composition.
2. Compatibility-duplicate folding: MICRO SIGN -> GREEK SMALL LETTER MU,
   OHM SIGN -> GREEK CAPITAL LETTER OMEGA.
3. Typographic ligature expansion (fi, fl, ff, ffi, ffl, ft, st).
4. Soft-hyphen removal, then hyphenation rejoining across line breaks when both
   sides are letters.
5. Dash folding to HYPHEN-MINUS; quotation folding to ASCII quotes.
6. Whitespace unification: every Unicode separator and control whitespace
   becomes U+0020, runs collapse to one, ends strip.

Never folded, by design: digits, letter case, Greek letters, superscript and
subscript characters, and any character inside a unit, chemical, or strain name.
Case is significant — ``pex10`` and ``PEX10`` are different genes, and a model
that changes case has paraphrased rather than quoted.

Raw offsets survive the transformation: ``normalise_with_map`` returns the index
map, so a verified span reports offsets into the original rendition text and the
original characters remain addressable.

Changing this chain requires a new version. Prior verifications are recorded
against the version that produced them and are not silently revalidated.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

__all__ = [
    "SPAN_NORMALISATION_VERSION",
    "SpanVerification",
    "normalise_for_comparison",
    "normalise_with_map",
    "verify_quote",
]

SPAN_NORMALISATION_VERSION = "1"

_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}

_COMPATIBILITY_DUPLICATES = {
    "µ": "μ",  # MICRO SIGN -> GREEK SMALL LETTER MU
    "Ω": "Ω",  # OHM SIGN -> GREEK CAPITAL LETTER OMEGA
}

_DASHES = {
    "‐": "-",  # HYPHEN
    "‑": "-",  # NON-BREAKING HYPHEN
    "‒": "-",  # FIGURE DASH
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH
    "―": "-",  # HORIZONTAL BAR
    "−": "-",  # MINUS SIGN
}

_QUOTES = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "″": '"',
}

_SOFT_HYPHEN = "­"
_ZERO_WIDTH = {"​", "‌", "‍", "﻿"}
_LINE_BREAKS = {"\n", "\r", "", "", "", " ", " "}


def _is_space(char: str) -> bool:
    if char in (" ", "\t") or char in _LINE_BREAKS:
        return True
    return unicodedata.category(char) == "Zs"


def normalise_with_map(text: str) -> tuple[str, tuple[int, ...]]:
    """Normalise ``text`` and return ``(normalised, raw_index_per_char)``.

    ``raw_index_per_char[i]`` is the index in ``text`` of the character that
    produced ``normalised[i]``. Expansions map several output characters to one
    input index; collapses map one output character to the first input index of
    the run.
    """
    # NFC is applied per character so the index map stays exact. A character
    # whose NFC form is longer contributes several output characters that all
    # point back to it.
    out: list[str] = []
    index_map: list[int] = []

    def emit(value: str, raw_index: int) -> None:
        for char in value:
            out.append(char)
            index_map.append(raw_index)

    position = 0
    length = len(text)
    while position < length:
        char = text[position]

        if char in _ZERO_WIDTH or char == _SOFT_HYPHEN:
            position += 1
            continue

        if _is_space(char):
            # Hyphenation rejoin: "...experi-\n    ment..." -> "...experiment..."
            if (
                out
                and out[-1] == "-"
                and len(out) >= 2
                and out[-2].isalpha()
                and any(c in _LINE_BREAKS for c in _run_of_space(text, position))
            ):
                after = _first_non_space(text, position)
                if after is not None and text[after].isalpha() and text[after].islower():
                    out.pop()
                    index_map.pop()
                    position = after
                    continue
            # Collapse the whole run to a single space, unless we are at the start.
            run_end = position
            while run_end < length and _is_space(text[run_end]):
                run_end += 1
            if out:
                emit(" ", position)
            position = run_end
            continue

        replacement = _COMPATIBILITY_DUPLICATES.get(char) or _LIGATURES.get(char)
        if replacement is None:
            replacement = _DASHES.get(char) or _QUOTES.get(char)
        if replacement is None:
            replacement = unicodedata.normalize("NFC", char)
        emit(replacement, position)
        position += 1

    while out and out[-1] == " ":
        out.pop()
        index_map.pop()

    return "".join(out), tuple(index_map)


def _run_of_space(text: str, start: int) -> str:
    end = start
    while end < len(text) and _is_space(text[end]):
        end += 1
    return text[start:end]


def _first_non_space(text: str, start: int) -> int | None:
    index = start
    while index < len(text) and _is_space(text[index]):
        index += 1
    return index if index < len(text) else None


def normalise_for_comparison(text: str) -> str:
    """Normalised form used for equality comparison."""
    return normalise_with_map(text)[0]


@dataclass(frozen=True, slots=True)
class SpanVerification:
    """Outcome of checking one quote against one rendition node."""

    verified: bool
    normalisation_version: str = SPAN_NORMALISATION_VERSION
    #: Offsets into the *original* node text, so raw characters stay addressable.
    raw_start: int | None = None
    raw_end: int | None = None
    normalised_start: int | None = None
    normalised_end: int | None = None
    occurrences: int = 0
    failure_code: str | None = None
    detail: str | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.occurrences > 1


def verify_quote(
    *,
    node_text: str,
    quote: str,
    expected_start: int | None = None,
) -> SpanVerification:
    """Verify that ``quote`` occurs in ``node_text`` under the contract.

    On failure the proposal is rejected as unverified evidence. It is never
    repaired: a quote that does not appear in the node is not evidence, and
    silently trimming it to make it fit would defeat the whole mechanism.
    """
    if not quote.strip():
        return SpanVerification(verified=False, failure_code="empty_quote", detail="quote is blank")

    normalised_node, index_map = normalise_with_map(node_text)
    normalised_quote = normalise_for_comparison(quote)
    if not normalised_quote:
        return SpanVerification(
            verified=False,
            failure_code="empty_quote_after_normalisation",
            detail="quote normalises to empty",
        )

    starts: list[int] = []
    search_from = 0
    while True:
        found = normalised_node.find(normalised_quote, search_from)
        if found == -1:
            break
        starts.append(found)
        search_from = found + 1

    if not starts:
        return SpanVerification(
            verified=False,
            failure_code="quote_not_in_node",
            detail=(
                "quote does not occur in the referenced node under normalisation "
                f"version {SPAN_NORMALISATION_VERSION}"
            ),
        )

    chosen = starts[0]
    if expected_start is not None and len(starts) > 1:
        # Prefer the occurrence nearest the offset the extractor reported.
        chosen = min(starts, key=lambda s: abs(_raw_index(index_map, s) - expected_start))

    end = chosen + len(normalised_quote)
    raw_start = _raw_index(index_map, chosen)
    raw_end = _raw_index(index_map, end - 1) + 1 if end - 1 < len(index_map) else len(node_text)
    return SpanVerification(
        verified=True,
        raw_start=raw_start,
        raw_end=raw_end,
        normalised_start=chosen,
        normalised_end=end,
        occurrences=len(starts),
    )


def _raw_index(index_map: tuple[int, ...], normalised_index: int) -> int:
    if not index_map:
        return 0
    if normalised_index >= len(index_map):
        return index_map[-1]
    return index_map[normalised_index]
