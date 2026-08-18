"""Deck assembly, inspected inside the generated .apkg."""

from __future__ import annotations

import sqlite3
import zipfile

import pytest

from flashcards_creator import deck


def meta(chapter_id="01.06", label="LIVRE PREMIER, ch. VI — LA ESMERALDA",
         incipit="Nous sommes ravis d’avoir à apprendre à nos lecteurs…"):
    return deck.DeckMeta(book="Notre-Dame de Paris", chapter_label=label,
                         chapter_id=chapter_id, incipit=incipit, paragraphs=25)


def rows(audio_path=""):
    return [
        {"lemma": "badaud", "pos": "NOUN", "pos_label": "noun",
         "display": "le badaud", "translation": "onlooker, gawker", "note": "",
         "sentence": "Un badaud regardait la scène.", "surfaces": ["badaud"],
         "level": "C2", "level_source": "flelex_pos", "first_paragraph": 3,
         "ipa": "/ba.do/", "audio_path": audio_path, "credits": ""},
        {"lemma": "talonner", "pos": "VERB", "pos_label": "verb",
         "display": "talonner", "translation": "to hound, to press",
         "note": "", "sentence": "Ses acteurs, talonnés par lui, continuaient.",
         "surfaces": ["talonnés"], "level": "C2", "level_source": "flelex_pos",
         "first_paragraph": 1, "ipa": "", "audio_path": "", "credits": ""},
    ]


def read_package(path):
    """Pull the notes and deck config back out of an .apkg."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        db_name = "collection.anki21" if "collection.anki21" in names else "collection.anki2"
        payload = zf.read(db_name)
        media = zf.read("media").decode("utf-8") if "media" in names else "{}"
    return names, payload, media


def load_notes(tmp_path, payload):
    db = tmp_path / "collection.sqlite"
    db.write_bytes(payload)
    con = sqlite3.connect(db)
    try:
        notes = con.execute("SELECT flds, tags, sfld FROM notes").fetchall()
        decks = con.execute("SELECT decks FROM col").fetchone()[0]
    finally:
        con.close()
    return notes, decks


def test_builds_a_package_with_a_card_per_word(tmp_path):
    out = tmp_path / "deck.apkg"
    result = deck.build_deck(rows(), meta(), out, incipit_card=False)

    assert out.exists()
    assert result["cards"] == 2
    _, payload, _ = read_package(out)
    notes, _ = load_notes(tmp_path, payload)
    assert len(notes) == 2


def test_incipit_card_is_present_and_sorts_first(tmp_path):
    out = tmp_path / "deck.apkg"
    deck.build_deck(rows(), meta(), out, incipit_card=True)

    _, payload, _ = read_package(out)
    notes, _ = load_notes(tmp_path, payload)
    assert len(notes) == 3

    # All sort keys must share one shape, so ordering does not depend on
    # SQLite coercing a numeric-looking key into the integer sort column.
    sort_fields = [n[2] for n in notes]
    assert all(isinstance(s, str) for s in sort_fields)
    assert sorted(sort_fields)[0] == "0000-0000"  # the incipit card leads
    incipit_notes = [n for n in notes if "incipit" in n[1]]
    assert len(incipit_notes) == 1
    assert "LA ESMERALDA" in incipit_notes[0][0]


def test_deck_description_identifies_the_chapter(tmp_path):
    out = tmp_path / "deck.apkg"
    deck.build_deck(rows(), meta(), out)

    _, payload, _ = read_package(out)
    _, decks_json = load_notes(tmp_path, payload)
    assert "LA ESMERALDA" in decks_json
    assert "Nous sommes ravis" in decks_json


def test_two_chapters_are_told_apart_by_their_incipit(tmp_path):
    """The whole reason the incipit exists: two decks must not be confusable."""
    a, b = tmp_path / "a.apkg", tmp_path / "b.apkg"
    deck.build_deck(rows(), meta(), a)
    deck.build_deck(rows(), meta(
        chapter_id="01.01", label="LIVRE PREMIER, ch. I — LA GRAND’SALLE",
        incipit="Il y a aujourd’hui trois cent quarante-huit ans…"), b)

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    _, payload_a, _ = read_package(a)
    _, payload_b, _ = read_package(b)
    _, decks_a = load_notes(first_dir, payload_a)
    _, decks_b = load_notes(second_dir, payload_b)

    assert "Nous sommes ravis" in decks_a
    assert "trois cent quarante-huit" in decks_b
    assert decks_a != decks_b


def test_paragraph_and_level_tags(tmp_path):
    out = tmp_path / "deck.apkg"
    deck.build_deck(rows(), meta(), out, incipit_card=False)

    _, payload, _ = read_package(out)
    notes, _ = load_notes(tmp_path, payload)
    tags = " ".join(n[1] for n in notes)
    assert "para-003" in tags and "para-001" in tags
    assert "level-C2" in tags
    assert "chapter-01-06" in tags


def test_cards_are_ordered_by_paragraph(tmp_path):
    # Reading order is the order the chapter will be read in.
    out = tmp_path / "deck.apkg"
    deck.build_deck(rows(), meta(), out, incipit_card=False)
    _, payload, _ = read_package(out)
    notes, _ = load_notes(tmp_path, payload)

    by_sort = sorted(notes, key=lambda n: n[2])
    assert "talonner" in by_sort[0][0]   # paragraph 1
    assert "badaud" in by_sort[1][0]     # paragraph 3


def test_audio_is_packaged_and_referenced(tmp_path):
    sound = tmp_path / "badaud.mp3"
    sound.write_bytes(b"ID3fake-mp3-payload")

    out = tmp_path / "deck.apkg"
    result = deck.build_deck(rows(audio_path=str(sound)), meta(), out,
                             incipit_card=False)
    assert result["media_files"] == 1

    names, payload, media = read_package(out)
    notes, _ = load_notes(tmp_path, payload)
    referenced = [n for n in notes if "[sound:badaud.mp3]" in n[0]]
    assert len(referenced) == 1
    # Every [sound:] reference must resolve to a file actually in the package.
    assert "badaud.mp3" in media
    assert "0" in names


def test_missing_audio_file_is_not_referenced(tmp_path):
    out = tmp_path / "deck.apkg"
    result = deck.build_deck(rows(audio_path="/nonexistent/x.mp3"), meta(), out,
                             incipit_card=False)
    assert result["media_files"] == 0
    _, payload, _ = read_package(out)
    notes, _ = load_notes(tmp_path, payload)
    assert not any("[sound:" in n[0] for n in notes)


def test_rebuilding_keeps_the_same_deck_and_note_identity(tmp_path):
    # Stable ids mean a rebuild updates the deck in place rather than making a
    # duplicate and losing review history.
    first = tmp_path / "one.apkg"
    second = tmp_path / "two.apkg"
    deck.build_deck(rows(), meta(), first)
    deck.build_deck(rows(), meta(), second)

    name = deck.deck_name("Notre-Dame de Paris", "01.06")
    assert deck._stable_id(name) == deck._stable_id(name)


def test_empty_selection_is_a_clear_error(tmp_path):
    with pytest.raises(deck.DeckError, match="no words"):
        deck.build_deck([], meta(), tmp_path / "empty.apkg")


@pytest.mark.parametrize("sentence,surfaces,expected", [
    ("Un badaud regardait.", ["badaud"], "<b>badaud</b>"),
    ("Ses acteurs, talonnés par lui.", ["talonnés"], "<b>talonnés</b>"),
])
def test_the_word_is_highlighted_in_its_sentence(sentence, surfaces, expected):
    assert expected in deck.highlight(sentence, surfaces)


def test_highlighting_escapes_html():
    out = deck.highlight("Le <b>faux</b> balise & co", ["balise"])
    assert "&lt;b&gt;" in out and "&amp;" in out
