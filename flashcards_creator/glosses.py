"""Dictionary lookups: English glosses, gender, IPA, audio URL.

Sourced from the local Wiktionary index built by scripts/fetch_data.py, which
reduces the Kaikki extract of English Wiktionary.

Coverage is good but not total. That extract carries French words with English
definitions, and its French coverage is narrower than the French Wiktionary's
for technical vocabulary -- ``carguer`` (to furl a sail) has no entry, for
instance. Missing entries are not fatal: sense-picking can still translate from
the sentence, and audio has a live fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import lexicon

# A noun's gender is part of the word. Cards show the article, not a label.
ARTICLES = {"m": "le", "f": "la"}

# POS shown on the card, in English, since the rest of the card's metadata is.
POS_LABELS = {
    "NOUN": "noun", "VERB": "verb", "ADJ": "adjective", "ADV": "adverb",
    "INTJ": "interjection", "PROPN": "proper noun", "NUM": "numeral",
    "PRON": "pronoun", "ADP": "preposition", "CCONJ": "conjunction",
}


@dataclass
class Entry:
    lemma: str
    pos: str
    glosses: list[str] = field(default_factory=list)
    gender: str = ""
    ipa: str = ""
    audio_url: str = ""
    audio_credit: str = ""

    @property
    def found(self) -> bool:
        return bool(self.glosses or self.ipa or self.audio_url)

    @property
    def display(self) -> str:
        """Headword as it should appear on the card, e.g. 'le vaisseau'.

        Elision matters: 'le eau' would be wrong, so a vowel-initial noun takes
        l' regardless of gender.
        """
        article = ARTICLES.get(self.gender)
        if self.pos != "NOUN" or not article:
            return self.lemma
        if self.lemma[:1].lower() in "aeiouyéèêàâîôû" or self.lemma[:1].lower() == "h":
            return f"l'{self.lemma}"
        return f"{article} {self.lemma}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "glosses": self.glosses,
            "gender": self.gender,
            "ipa": self.ipa,
            "audio_url": self.audio_url,
            "audio_credit": self.audio_credit,
            "display": self.display,
            "pos_label": POS_LABELS.get(self.pos, self.pos.lower()),
        }


def lookup(lemma: str, pos: str) -> Entry:
    """Find a word in the local index, falling back across parts of speech.

    An exact (lemma, POS) hit is preferred; failing that any POS for the lemma
    is better than nothing, since the tagger may simply have been wrong.
    """
    index = lexicon.wiktionary()
    entry = Entry(lemma=lemma, pos=pos)

    by_pos = index.get(lemma) or index.get(lemma.capitalize()) or {}
    if not by_pos:
        return entry

    data = by_pos.get(pos)
    if data is None:
        # Prefer a reading that at least has definitions.
        ranked = sorted(by_pos.values(), key=lambda d: -len(d.get("glosses") or []))
        data = ranked[0] if ranked else None
    if data is None:
        return entry

    entry.glosses = list(data.get("glosses") or [])
    entry.gender = data.get("gender") or ""
    entry.ipa = data.get("ipa") or ""
    entry.audio_url = data.get("audio") or ""
    entry.audio_credit = data.get("audio_credit") or ""

    # Gender can be absent from the matched reading but present on another.
    if entry.pos == "NOUN" and not entry.gender:
        for other in by_pos.values():
            if other.get("gender"):
                entry.gender = other["gender"]
                break
    return entry


def coverage(rows: list[dict]) -> dict[str, Any]:
    """How much of a word list the dictionary actually covers.

    Reported before building so a thin chapter is visible rather than
    discovered as a deck full of empty cards.
    """
    total = len(rows)
    glossed = sum(1 for r in rows if r.get("glosses"))
    audio = sum(1 for r in rows if r.get("audio_url"))
    return {
        "total": total,
        "with_glosses": glossed,
        "with_audio": audio,
        "missing_glosses": [r["lemma"] for r in rows if not r.get("glosses")],
    }
