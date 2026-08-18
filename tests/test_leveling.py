"""The CEFR cascade and its threshold logic."""

from __future__ import annotations

import pytest

from flashcards_creator import leveling
from flashcards_creator.leveling import UNKNOWN, at_or_above

requires_data = pytest.mark.skipif(
    not __import__("flashcards_creator.paths", fromlist=["paths"]).FLELEX_INDEX.exists(),
    reason="reference data not built (run scripts/fetch_data.py)")


@pytest.mark.parametrize("level,threshold,expected", [
    ("B2", "B2", True),
    ("C1", "B2", True),
    ("C2", "B2", True),
    ("B1", "B2", False),
    ("A1", "B2", False),
    ("C1", "C1", True),
    ("B2", "C1", False),
])
def test_threshold_comparison(level, threshold, expected):
    assert at_or_above(level, threshold) is expected


def test_unknown_is_excluded_by_default_and_optional():
    # Words neither lexicon recognises are mostly tagger noise, so they are
    # dropped unless explicitly asked for.
    assert at_or_above(UNKNOWN, "B2") is False
    assert at_or_above(UNKNOWN, "B2", include_unknown=True) is True


def test_pos_mapping_covers_the_content_tags():
    for upos in ("NOUN", "VERB", "ADJ", "ADV", "INTJ"):
        assert upos in leveling.UPOS_TO_TREETAGGER
    assert leveling.UPOS_TO_TREETAGGER["NOUN"] == "NOM"
    assert leveling.UPOS_TO_TREETAGGER["VERB"] == "VER"
    assert leveling.UPOS_TO_TREETAGGER["AUX"] == "VER"


@requires_data
def test_tier1_exact_pos_match_is_authoritative():
    result = leveling.level_for("badaud", "NOUN")
    assert result.source == "flelex_pos"
    assert result.level in leveling.LEVELS


@requires_data
def test_tier3_estimates_off_list_words_from_frequency():
    # apothicaire is absent from FLELex but known to Lexique.
    result = leveling.level_for("apothicaire", "NOUN")
    assert result.source == "freq_estimate"
    assert result.modern_freq is not None


@requires_data
def test_off_list_words_are_never_reported_below_the_floor():
    # A word outside FLELex is outside the standard curriculum, so however
    # common it looks it should not come back as beginner vocabulary.
    from flashcards_creator import lexicon
    floor = lexicon.freq_bands().get("off_list_floor", "B2")
    for lemma in ("apothicaire", "butor", "fisc"):
        result = leveling.level_for(lemma, "NOUN")
        if result.source == "freq_estimate":
            assert at_or_above(result.level, floor)


@requires_data
def test_tier4_reports_unknown_for_words_in_neither_lexicon():
    result = leveling.level_for("zzzqxnotaword", "NOUN")
    assert result.level == UNKNOWN
    assert result.source == "unknown"


@requires_data
def test_every_word_records_how_it_was_judged():
    rows = leveling.level_candidates([
        {"lemma": "badaud", "pos": "NOUN", "count": 1},
        {"lemma": "apothicaire", "pos": "NOUN", "count": 2},
        {"lemma": "zzzqxnotaword", "pos": "NOUN", "count": 1},
    ])
    assert len(rows) == 3
    for row in rows:
        assert row["level_source"] in {
            "flelex_pos", "flelex_lemma", "freq_estimate", "unknown"}
        assert "passes_threshold" in row and "already_known" in row


@requires_data
def test_leveling_filters_nothing():
    # Stage 2 flags; stage 3 decides. Selection needs to see rejected words in
    # order to justify rejecting them, and to let the user overrule.
    candidates = [
        {"lemma": "maison", "pos": "NOUN", "count": 1},   # A1
        {"lemma": "badaud", "pos": "NOUN", "count": 1},   # advanced
    ]
    rows = leveling.level_candidates(candidates, threshold="B2")
    assert len(rows) == len(candidates)
    assert any(not r["passes_threshold"] for r in rows)


@requires_data
def test_already_known_words_are_flagged_not_removed():
    rows = leveling.level_candidates(
        [{"lemma": "badaud", "pos": "NOUN", "count": 1}],
        known={"badaud|NOUN": {"lemma": "badaud"}})
    assert len(rows) == 1
    assert rows[0]["already_known"] is True


def test_frequency_estimate_reports_its_own_confidence():
    # Frequency cannot separate A2 from B1 or C1 from C2, so the estimator has
    # to admit how uncertain it is rather than assert a level flatly.
    level, agreement = leveling.estimate_from_frequency(None)
    assert level == UNKNOWN and agreement is None
