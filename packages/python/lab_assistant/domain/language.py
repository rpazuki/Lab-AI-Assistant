"""A paper's language: as a source records it, or as its title reads.

Sources write a language three ways: PubMed and Europe PMC as three-letter codes
(``eng``, ``spa``), Crossref and OpenAlex as two-letter ones (``en``, ``es``), and
now and then as a name (``English``). :func:`normalise_language` brings all of
them to the two-letter code, so one filter finds a paper whichever source it
came from.

Many records carry no language at all. For those :func:`detect_language` reads
the title (and the abstract, when there is one) and names a language only when
the text says so clearly: a script that only some languages use (Cyrillic,
Chinese, Japanese, Korean, Arabic, Greek, Hebrew, Thai), or enough of one
language's common short words. A short title it cannot place gives ``None``,
not a guess. A detected language is marked as detected, so a person can tell it
from one a source recorded.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "LANGUAGE_NAMES",
    "UNKNOWN_LANGUAGE",
    "detect_language",
    "language_name",
    "normalise_language",
]

#: What a filter writes for "no language recorded or detected".
UNKNOWN_LANGUAGE = "unknown"

#: Two-letter code -> English name, for the languages a filter offers by name.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "fa": "Persian",
    "he": "Hebrew",
    "el": "Greek",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sr": "Serbian",
    "sl": "Slovenian",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "ca": "Catalan",
    "gl": "Galician",
    "eu": "Basque",
    "id": "Indonesian",
    "ms": "Malay",
    "th": "Thai",
    "vi": "Vietnamese",
    "hi": "Hindi",
    "af": "Afrikaans",
}

#: Three-letter codes (ISO 639-2, bibliographic and terminology) -> two-letter.
_THREE_LETTER: dict[str, str] = {
    "eng": "en",
    "spa": "es",
    "por": "pt",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "ita": "it",
    "dut": "nl",
    "nld": "nl",
    "pol": "pl",
    "tur": "tr",
    "rus": "ru",
    "ukr": "uk",
    "chi": "zh",
    "zho": "zh",
    "jpn": "ja",
    "kor": "ko",
    "ara": "ar",
    "per": "fa",
    "fas": "fa",
    "heb": "he",
    "gre": "el",
    "ell": "el",
    "cze": "cs",
    "ces": "cs",
    "slo": "sk",
    "slk": "sk",
    "hun": "hu",
    "rum": "ro",
    "ron": "ro",
    "bul": "bg",
    "hrv": "hr",
    "srp": "sr",
    "slv": "sl",
    "swe": "sv",
    "nor": "no",
    "nob": "no",
    "dan": "da",
    "fin": "fi",
    "lit": "lt",
    "lav": "lv",
    "est": "et",
    "cat": "ca",
    "glg": "gl",
    "baq": "eu",
    "eus": "eu",
    "ind": "id",
    "may": "ms",
    "msa": "ms",
    "tha": "th",
    "vie": "vi",
    "hin": "hi",
    "afr": "af",
}

#: "Undetermined" and "multiple": no single language to file a paper under.
_NO_LANGUAGE = frozenset({"und", "mul", "zxx", "mis", ""})

_BY_NAME = {name.casefold(): code for code, name in LANGUAGE_NAMES.items()}


def normalise_language(value: object) -> str | None:
    """A source's language as a two-letter code; ``None`` when it names none."""
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace("_", "-")
    if text in _NO_LANGUAGE:
        return None
    base = text.split("-", 1)[0]  # "en-gb" -> "en"
    if len(base) == 2 and base.isalpha():
        return base
    if base in _THREE_LETTER:
        return _THREE_LETTER[base]
    if base in _NO_LANGUAGE:
        return None
    return _BY_NAME.get(text)


def language_name(code: str | None) -> str:
    if not code or code == UNKNOWN_LANGUAGE:
        return "Unknown"
    return LANGUAGE_NAMES.get(code, code)


# ── detection ─────────────────────────────────────────────────────────────

#: Scripts that settle the language (or nearly: Cyrillic is Russian unless the
#: Ukrainian letters say otherwise).
_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("HIRAGANA", "ja"),
    ("KATAKANA", "ja"),
    ("HANGUL", "ko"),
    ("CJK", "zh"),
    ("CYRILLIC", "ru"),
    ("ARABIC", "ar"),
    ("GREEK", "el"),
    ("HEBREW", "he"),
    ("THAI", "th"),
    ("DEVANAGARI", "hi"),
)

#: Common short words of each language. A word several share ("en", "in")
#: counts for each of them; the words only one uses decide between them.
_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "of",
            "and",
            "in",
            "for",
            "with",
            "from",
            "by",
            "on",
            "to",
            "an",
            "is",
            "are",
            "as",
            "at",
            "its",
            "using",
            "under",
            "during",
            "between",
            "into",
            "via",
            "study",
            "effect",
            "effects",
            "analysis",
        ]
    ),
    "es": frozenset(
        [
            "de",
            "la",
            "al",
            "lo",
            "el",
            "los",
            "las",
            "del",
            "y",
            "en",
            "con",
            "para",
            "por",
            "una",
            "uno",
            "su",
            "sus",
            "sobre",
            "entre",
            "como",
            "mediante",
            "estudio",
            "efecto",
            "análisis",
            "producción",
        ]
    ),
    "pt": frozenset(
        [
            "de",
            "no",
            "na",
            "nos",
            "nas",
            "ao",
            "o",
            "os",
            "as",
            "do",
            "da",
            "dos",
            "das",
            "e",
            "em",
            "com",
            "para",
            "por",
            "uma",
            "um",
            "seu",
            "sua",
            "sobre",
            "entre",
            "como",
            "através",
            "estudo",
            "efeito",
            "análise",
            "produção",
        ]
    ),
    "fr": frozenset(
        [
            "de",
            "la",
            "au",
            "aux",
            "le",
            "les",
            "des",
            "du",
            "et",
            "en",
            "avec",
            "pour",
            "par",
            "une",
            "un",
            "sur",
            "dans",
            "entre",
            "comme",
            "chez",
            "étude",
            "effet",
            "analyse",
        ]
    ),
    "de": frozenset(
        [
            "der",
            "die",
            "das",
            "den",
            "dem",
            "des",
            "und",
            "in",
            "mit",
            "für",
            "von",
            "zur",
            "zum",
            "bei",
            "auf",
            "über",
            "durch",
            "eine",
            "einer",
            "ein",
            "untersuchung",
            "wirkung",
            "analyse",
        ]
    ),
    "it": frozenset(
        [
            "di",
            "la",
            "al",
            "il",
            "lo",
            "gli",
            "della",
            "delle",
            "dei",
            "degli",
            "e",
            "in",
            "con",
            "per",
            "una",
            "uno",
            "su",
            "tra",
            "come",
            "nel",
            "nella",
            "studio",
            "effetto",
            "analisi",
        ]
    ),
    "nl": frozenset(
        [
            "het",
            "de",
            "van",
            "een",
            "en",
            "in",
            "met",
            "voor",
            "door",
            "op",
            "bij",
            "naar",
            "onderzoek",
        ]
    ),
    "pl": frozenset(
        ["i", "w", "z", "na", "do", "dla", "oraz", "przez", "od", "jako", "badanie", "wpływ"]
    ),
}

_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Word endings only one of them uses ("producción", "produção", "Untersuchung").
_ENDINGS: tuple[tuple[str, str], ...] = (
    ("ción", "es"),
    ("ciones", "es"),
    ("ção", "pt"),
    ("ções", "pt"),
    ("zione", "it"),
    ("zioni", "it"),
    ("ung", "de"),
    ("ungen", "de"),
)

#: Letters only one of the Latin-script languages above writes.
_MARKS: dict[str, str] = {"ñ": "es", "ã": "pt", "õ": "pt", "ß": "de", "ł": "pl", "ą": "pl"}


def detect_language(text: str | None) -> str | None:
    """The language ``text`` is written in, when it says so clearly; else ``None``."""
    if not text:
        return None
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return None
    scripts: dict[str, int] = {}
    for ch in letters:
        if ch.isascii():
            continue
        name = unicodedata.name(ch, "")
        for marker, code in _SCRIPTS:
            if marker in name:
                scripts[code] = scripts.get(code, 0) + 1
                break
    if scripts:
        code, count = max(scripts.items(), key=lambda kv: kv[1])
        if count >= max(3, len(letters) // 3):
            # Kana anywhere means Japanese, even among Chinese characters.
            if "ja" in scripts and code == "zh":
                return "ja"
            if code == "ru" and any(ch in text for ch in "іїєґІЇЄҐ"):
                return "uk"
            return code

    words = [word.casefold() for word in _LETTERS.findall(text)]
    if not words:
        return None
    scores = {code: sum(1 for word in words if word in vocab) for code, vocab in _WORDS.items()}
    for mark, code in _MARKS.items():
        if mark in text.casefold():
            scores[code] = scores.get(code, 0) + 2
    for word in words:
        for ending, code in _ENDINGS:
            if word.endswith(ending):
                scores[code] = scores.get(code, 0) + 2
                break
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    # Two clear hits, and ahead of the next language: a title of names and
    # technical terms ("Yarrowia lipolytica CRISPR") is left undecided.
    if top >= 2 and top > second:
        return best
    return None
