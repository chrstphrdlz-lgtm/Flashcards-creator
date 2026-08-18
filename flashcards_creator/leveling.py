"""Stage 2: assign a CEFR level to every candidate word.

A four-tier cascade. Every word records which tier judged it, so a card can
always show *how* its level was decided rather than asserting it flatly.

  1  flelex_pos    exact match on (lemma, part of speech)   -- authoritative
  2  flelex_lemma  lemma match, dominant reading            -- authoritative
  3  freq_estimate calibrated from modern book frequency    -- coarse
  4  unknown       in neither lexicon                       -- assumed hard

Tiers 1 and 2 come from FLELex-Beacco, built by UCLouvain from French-as-a-
foreign-language textbooks and graded readers combined with Beacco's expert
CEFR referentials. Tier 3 is deliberately coarse: measured on FLELex itself,
frequency cannot separate A2 from B1, nor C1 from C2, so it reports a
confidence alongside the level rather than pretending otherwise.

This stage filters nothing. It flags. Stage 3 decides what to keep, and it
needs the full picture to do that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import lexicon

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
LEVEL_INDEX = {lv: i for i, lv in enumerate(LEVELS)}

UNKNOWN = "UNKNOWN"

# spaCy universal POS -> FLELex's TreeTagger tagset.
UPOS_TO_TREETAGGER = {
    "NOUN": "NOM",
    "PROPN": "NOM",
    "VERB": "VER",
    "AUX": "VER",
    "ADJ": "ADJ",
    "ADV": "ADV",
    "PRON": "PRO",
    "INTJ": "INT",
    "ADP": "PRP",
    "CCONJ": "KON",
    "SCONJ": "KON",
    "DET": "DET:ART",
    "NUM": "NUM",
}


@dataclass
class Level:
    level: str
    source: str
    confidence: float | None = None
    modern_freq: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "level_source": self.source,
            "level_confidence": self.confidence,
            "modern_freq": self.modern_freq,
        }


def at_or_above(level: str, threshold: str, include_unknown: bool = False) -> bool:
    """Is `level` at least `threshold`?

    UNKNOWN means neither lexicon recognised the word. In practice that bucket
    is dominated by tagger noise rather than real vocabulary, so it is excluded
    by default; pass ``include_unknown`` to keep it. Excluded words still
    appear in the stage-2 artifact, flagged rather than removed, so selection
    can overrule this.
    """
    if level == UNKNOWN:
        return include_unknown
    if level not in LEVEL_INDEX or threshold not in LEVEL_INDEX:
        return False
    return LEVEL_INDEX[level] >= LEVEL_INDEX[threshold]


def estimate_from_frequency(freq: float | None) -> tuple[str, float | None]:
    """Place a word on the calibrated frequency scale.

    Returns (level, agreement). Agreement is the share of FLELex words in that
    frequency bin which actually sit at the reported level -- around 0.5 at
    best, and as low as 0.12 in the A2 range. It is surfaced rather than hidden
    because it is the honest measure of how much this tier can be trusted.
    """
    if freq is None or freq <= 0:
        return UNKNOWN, None

    bands = lexicon.freq_bands()
    for band in bands.get("bins", []):
        lo = band["min_freq"]
        hi = band["max_freq"]
        if freq >= lo and (hi is None or freq < hi):
            return band["level"], band.get("agreement")
    return UNKNOWN, None


def level_for(lemma: str, pos: str) -> Level:
    """Run the cascade for one (lemma, POS) pair."""
    lemma = lemma.lower()
    fle = lexicon.flelex()
    modern = lexicon.modern_frequency(lemma)

    entries = fle.get(lemma)
    if entries:
        # Tier 1: the POS the tagger actually saw.
        tag = UPOS_TO_TREETAGGER.get(pos)
        if tag and tag in entries:
            return Level(entries[tag]["level"], "flelex_pos", 1.0, modern)

        # Tier 2: no POS match, so take the lemma's dominant reading.
        best = max(entries.values(), key=lambda e: e["freq"])
        return Level(best["level"], "flelex_lemma", 0.8, modern)

    # Tier 3: not in FLELex, estimate from modern book frequency, with a floor
    # -- absence from FLELex already means the word is off-curriculum.
    level, agreement = estimate_from_frequency(modern)
    if level != UNKNOWN:
        floor = lexicon.freq_bands().get("off_list_floor", "B2")
        if floor in LEVEL_INDEX and LEVEL_INDEX[level] < LEVEL_INDEX[floor]:
            level = floor
        return Level(level, "freq_estimate", agreement, modern)

    # Tier 4: nothing known about it.
    return Level(UNKNOWN, "unknown", None, modern)


def level_candidates(
    candidates: list[dict],
    threshold: str = "B2",
    known: dict[str, Any] | None = None,
    include_unknown: bool = False,
) -> list[dict]:
    """Annotate every candidate with its level and the two selection flags.

    ``passes_threshold`` and ``already_known`` are advisory. Nothing is removed
    here -- stage 3 needs to see the words it is rejecting in order to justify
    rejecting them, and so you can overrule it.
    """
    known = known or {}
    out = []
    for cand in candidates:
        lemma, pos = cand["lemma"], cand["pos"]
        level = level_for(lemma, pos)
        row = dict(cand)
        row.update(level.to_dict())
        row["passes_threshold"] = at_or_above(level.level, threshold, include_unknown)
        row["already_known"] = f"{lemma}|{pos}" in known
        out.append(row)

    # Hardest and most-repeated first, so a truncated review still sees the
    # words that matter most for reading the chapter.
    out.sort(key=lambda r: (
        -(LEVEL_INDEX.get(r["level"], len(LEVELS))),
        -r.get("count", 0),
        r["lemma"],
    ))
    return out


def summarise(rows: list[dict]) -> dict[str, Any]:
    """Counts by level and by source, for the stage-2 report."""
    by_level: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_level[row["level"]] = by_level.get(row["level"], 0) + 1
        by_source[row["level_source"]] = by_source.get(row["level_source"], 0) + 1

    ordered = {lv: by_level.get(lv, 0) for lv in LEVELS if by_level.get(lv)}
    if by_level.get(UNKNOWN):
        ordered[UNKNOWN] = by_level[UNKNOWN]

    return {
        "total": len(rows),
        "by_level": ordered,
        "by_source": by_source,
        "passing": sum(1 for r in rows if r["passes_threshold"]),
        "already_known": sum(1 for r in rows if r["already_known"]),
        "candidates": sum(1 for r in rows
                          if r["passes_threshold"] and not r["already_known"]),
    }
