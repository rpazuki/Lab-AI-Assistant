"""A paper's language, from what a source wrote or what its title says."""

from __future__ import annotations

from uuid import uuid4

import pytest

from lab_assistant.domain.language import detect_language, normalise_language
from lab_assistant.domain.review import summarise_work


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("eng", "en"),
        ("en", "en"),
        ("en-GB", "en"),
        ("spa", "es"),
        ("fre", "fr"),
        ("ger", "de"),
        ("chi", "zh"),
        ("English", "en"),
        ("und", None),
        ("mul", None),
        ("", None),
        (None, None),
    ],
)
def test_every_way_a_source_writes_a_language_is_one_code(value, code) -> None:
    assert normalise_language(value) == code


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Lipid production in Yarrowia lipolytica using glycerol", "en"),
        ("Producción de lípidos en Yarrowia lipolytica", "es"),
        ("Produção de lipídios por leveduras oleaginosas", "pt"),
        ("Étude des lipides chez la levure dans un bioréacteur", "fr"),
        ("Untersuchung der Lipidbildung in Hefen für die Industrie", "de"),
        ("Продукция липидов дрожжами Yarrowia", "ru"),
        ("解脂耶氏酵母的脂质生产研究", "zh"),
        ("ヤロウイア酵母による脂質の生産", "ja"),
        # Names and technical terms only: no language is claimed.
        ("Yarrowia lipolytica CRISPR", None),
        ("", None),
    ],
)
def test_a_title_names_its_language_only_when_it_is_clear(text, code) -> None:
    assert detect_language(text) == code


def test_a_recorded_language_wins_and_a_detected_one_is_marked() -> None:
    title = "Producción de lípidos en levaduras"
    recorded = summarise_work(uuid4(), uuid4(), {"title": title, "language": "eng"}, [])
    assert (recorded.language, recorded.language_detected) == ("en", False)
    detected = summarise_work(uuid4(), uuid4(), {"title": title}, [])
    assert (detected.language, detected.language_detected) == ("es", True)
    assert "levaduras" in (detected.text_search or "")
