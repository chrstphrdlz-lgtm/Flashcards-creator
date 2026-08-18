"""Loads the derived reference indexes built by scripts/fetch_data.py."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import paths


class LexiconError(Exception):
    """Raised when a reference index is missing or unreadable."""


def _load(path: Path, what: str) -> Any:
    if not path.exists():
        raise LexiconError(
            f"{what} index not found at {path}.\n"
            f"  Build the reference data first:  python scripts/fetch_data.py"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LexiconError(
            f"{path} is corrupt ({exc}). Rebuild it with "
            f"`python scripts/fetch_data.py --force`."
        ) from exc


@lru_cache(maxsize=1)
def flelex() -> dict[str, dict[str, dict]]:
    """lemma -> {treetagger_tag: {level, freq}} from FLELex-Beacco."""
    return _load(paths.FLELEX_INDEX, "FLELex")


@lru_cache(maxsize=1)
def lexique() -> dict[str, dict]:
    """{'forms': surface -> lemma, 'lemmas': lemma -> {pos, books, films}}."""
    return _load(paths.LEXIQUE_INDEX, "Lexique")


@lru_cache(maxsize=1)
def freq_bands() -> dict:
    """Calibrated frequency -> CEFR bands, from FLELex."""
    return _load(paths.FREQ_BANDS, "frequency band")


@lru_cache(maxsize=1)
def wiktionary() -> dict[str, dict[str, dict]]:
    """word -> {UPOS: {glosses, gender, ipa, audio, audio_credit}}.

    Optional: decks still build without it, just without glosses or audio.
    """
    if not paths.WIKTIONARY_INDEX.exists():
        return {}
    return _load(paths.WIKTIONARY_INDEX, "Wiktionary")


@lru_cache(maxsize=1)
def known_forms() -> set[str]:
    """Every lemma either reference lexicon recognises.

    Used by stage 1 to reject tokens that are neither French words nor
    recognised lemmas -- typically proper nouns the tagger missed.
    """
    lex = lexique()
    forms = set(lex["forms"].keys())
    forms.update(lex["lemmas"].keys())
    forms.update(flelex().keys())
    return forms


@lru_cache(maxsize=1)
def real_lemmas() -> set[str]:
    """Lemmas proper -- not inflected forms.

    Distinct from :func:`known_forms`, and the distinction matters: ``baissé``
    is a known *form* but not a lemma, so accepting it as one is how an
    unlemmatised participle sneaks through.
    """
    return set(lexique()["lemmas"].keys()) | set(flelex().keys())


# Lexique's POS tags -> spaCy universal POS.
_CGRAM_TO_UPOS = {
    "NOM": "NOUN", "VER": "VERB", "ADJ": "ADJ", "ADV": "ADV",
    "PRO": "PRON", "PRE": "ADP", "CON": "CCONJ", "ART": "DET",
}


def _corroborate_pos(lemma: str, pos: str) -> str:
    """Prefer Lexique's part of speech when the tagger's has no support.

    Only fires on evidence: the tagger's reading must be absent from FLELex
    while Lexique's is present. That keeps genuinely ambiguous words (``un
    souvenir`` / ``se souvenir``) alone and fixes the clear errors, which on
    19th-century prose means participles and rare nouns landing on ADV.
    """
    entry = lexique()["lemmas"].get(lemma)
    if not entry:
        return pos
    candidate = _CGRAM_TO_UPOS.get(entry.get("pos", "").split(":")[0])
    if not candidate or candidate == pos:
        return pos

    from .leveling import UPOS_TO_TREETAGGER  # local: avoids a circular import

    tags = flelex().get(lemma)
    if not tags:
        return pos
    if UPOS_TO_TREETAGGER.get(pos) in tags:
        return pos  # the tagger's reading is attested; leave it
    if UPOS_TO_TREETAGGER.get(candidate) in tags:
        return candidate
    return pos


def resolve_lemma(surface: str, lemma: str, pos: str) -> tuple[str, str, bool]:
    """Correct a lemma the tagger got wrong, using Lexique as a fallback.

    spaCy's French model is trained largely on contemporary text and stumbles
    on 19th-century literary forms: it leaves *passé simple* (``murmura``) and
    adjectival participles (``déchiquetée``) unlemmatised, and often mistags
    them as nouns. Lexique maps 125k inflected forms to their lemma, which
    recovers both the lemma and, where the tagger disagreed, the part of
    speech.

    Returns ``(lemma, pos, resolved)``; ``resolved`` is False when neither
    source recognises the word.
    """
    lemma = (lemma or "").lower().replace("’", "'")
    surface = (surface or "").lower().replace("’", "'")

    forms = lexique()["forms"]
    lemmas = lexique()["lemmas"]

    if lemma in real_lemmas():
        return lemma, _corroborate_pos(lemma, pos), True

    for probe in (surface, lemma):
        found = forms.get(probe)
        if not found or found not in real_lemmas():
            continue
        entry = lemmas.get(found)
        # Trust Lexique's POS only when it actually knows the lemma; the
        # tagger's guess was already shown to be unreliable for these words.
        corrected = _CGRAM_TO_UPOS.get((entry or {}).get("pos", "").split(":")[0], pos)
        return found, corrected, True

    return lemma, pos, False


def modern_frequency(lemma: str) -> float | None:
    """Occurrences per million in a corpus of modern books, or None.

    This is the signal that separates advanced-but-useful vocabulary from
    merely archaic vocabulary; CEFR level alone cannot tell them apart.
    """
    entry = lexique()["lemmas"].get(lemma)
    return entry["books"] if entry else None
