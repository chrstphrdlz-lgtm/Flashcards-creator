"""The seen-words store: what stops chapter 2 re-teaching chapter 1."""

from __future__ import annotations

from flashcards_creator import leveling, store


def words(*pairs):
    return [{"lemma": lemma, "pos": pos, "level": "C1"} for lemma, pos in pairs]


def test_recording_and_lookup(tmp_path):
    known = {}
    added = store.record(known, words(("badaud", "NOUN")), "Notre-Dame", "01.06")
    assert added == 1
    assert store.contains(known, "badaud", "NOUN")


def test_lookup_is_per_part_of_speech(tmp_path):
    # 'livre' the noun and 'livrer' the verb are different things to learn, so
    # carding one must not silently retire the other.
    known = {}
    store.record(known, words(("livre", "NOUN")), "b", "1")
    assert store.contains(known, "livre", "NOUN")
    assert not store.contains(known, "livre", "VERB")


def test_recording_is_idempotent_and_keeps_first_sighting(tmp_path):
    known = {}
    store.record(known, words(("badaud", "NOUN")), "Notre-Dame", "01.06")
    added = store.record(known, words(("badaud", "NOUN")), "Notre-Dame", "01.09")
    assert added == 0
    assert known["badaud|NOUN"]["chapter"] == "01.06"


def test_round_trips_through_disk(tmp_path):
    path = tmp_path / "known.json"
    known = {}
    store.record(known, words(("badaud", "NOUN"), ("butor", "NOUN")), "b", "1")
    store.save(known, path)
    assert store.load(path) == known


def test_a_corrupt_store_does_not_block_building(tmp_path):
    path = tmp_path / "known.json"
    path.write_text("{ this is not json", encoding="utf-8")
    # Worst case a few words get carded twice -- better than refusing to build.
    assert store.load(path) == {}


def test_missing_store_is_empty(tmp_path):
    assert store.load(tmp_path / "absent.json") == {}


def test_forget_lets_a_word_be_carded_again(tmp_path):
    known = {}
    store.record(known, words(("badaud", "NOUN")), "b", "1")
    assert store.forget(known, ["badaud"]) == 1
    assert not store.contains(known, "badaud", "NOUN")


def test_the_second_chapter_only_surfaces_new_words():
    """The feature this store exists for, end to end."""
    chapter_one = [
        {"lemma": "badaud", "pos": "NOUN", "count": 2},
        {"lemma": "talonner", "pos": "VERB", "count": 1},
    ]
    chapter_two = chapter_one + [{"lemma": "apothicaire", "pos": "NOUN", "count": 1}]

    known = {}
    first = leveling.level_candidates(chapter_one, known=known)
    kept_first = [r for r in first if r["passes_threshold"] and not r["already_known"]]
    store.record(known, kept_first, "Notre-Dame", "01.06")

    second = leveling.level_candidates(chapter_two, known=known)
    kept_second = [r for r in second
                   if r["passes_threshold"] and not r["already_known"]]

    repeated = {r["lemma"] for r in kept_first} & {r["lemma"] for r in kept_second}
    assert not repeated, f"chapter 2 re-teaches {repeated}"


def test_stats_summarise_the_store():
    known = {}
    store.record(known, words(("badaud", "NOUN"), ("butor", "NOUN")),
                 "Notre-Dame", "01.06")
    data = store.stats(known)
    assert data["total"] == 2
    assert data["by_level"]["C1"] == 2
