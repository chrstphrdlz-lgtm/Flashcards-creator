"""Chapter detection, against the real book it was built for.

Notre-Dame de Paris exercises every structural trap at once: two-level
nesting, divider stubs, and front/back matter in the same spine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flashcards_creator import ingest

EPUB = Path("books/notre-dame-de-paris.epub")

pytestmark = pytest.mark.skipif(
    not EPUB.exists(), reason="the Notre-Dame EPUB is not present")


@pytest.fixture(scope="module")
def book():
    return ingest.read_chapters(EPUB)


def test_finds_every_chapter_and_no_dividers(book):
    title, chapters = book
    assert title == "Notre-Dame de Paris"
    # 59 content chapters across 11 Livres. Anything more means the 11 divider
    # stubs ("LIVRE PREMIER" alone, ~35 chars) leaked in as empty chapters.
    assert len(chapters) == 59
    assert sorted({c.part for c in chapters}) == list(range(1, 12))


def test_excludes_front_and_back_matter(book):
    _, chapters = book
    titles = " ".join(c.title.lower() for c in chapters)
    for unwanted in ("colophon", "table", "note ajoutée"):
        assert unwanted not in titles


def test_paragraph_counts_match_the_source(book):
    _, chapters = book
    assert len(ingest.select_chapter(chapters, "1.1").paragraphs) == 139
    assert len(ingest.select_chapter(chapters, "1.6").paragraphs) == 25


def test_both_address_forms_resolve_alike(book):
    _, chapters = book
    assert ingest.select_chapter(chapters, "1.6") is ingest.select_chapter(chapters, "6")


def test_livre_heading_is_not_mistaken_for_the_chapter_title(book):
    _, chapters = book
    first = ingest.select_chapter(chapters, "1.1")
    assert first.title == "LA GRAND’SALLE"
    assert first.part_title == "LIVRE PREMIER"


def test_incipit_is_distinctive_per_chapter(book):
    _, chapters = book
    one = ingest.select_chapter(chapters, "1.1").incipit()
    six = ingest.select_chapter(chapters, "1.6").incipit()
    assert one.startswith("Il y a aujourd’hui trois cent quarante-huit ans")
    assert one != six
    # The whole point is telling two decks apart at a glance.
    assert len(one) <= 201 and len(six) <= 201


def test_unknown_chapter_is_reported_clearly(book):
    _, chapters = book
    with pytest.raises(ingest.IngestError, match="99"):
        ingest.select_chapter(chapters, "99")


def test_missing_file_is_reported_clearly():
    with pytest.raises(ingest.IngestError, match="no such file"):
        ingest.read_chapters(Path("books/does-not-exist.epub"))


@pytest.mark.parametrize("heading,expected", [
    ("LIVRE PREMIER", 1),
    ("LIVRE DEUXIÈME", 2),
    ("LIVRE ONZIÈME", 11),
    ("LIVRE IV", 4),
])
def test_part_numbering(heading, expected):
    assert ingest.part_number(heading) == expected


@pytest.mark.parametrize("heading,expected", [
    ("I", 1), ("IV", 4), ("VIII", 8), ("12", 12), ("Chapitre III", 3),
    ("LA GRAND’SALLE", None), ("MARIAGE DE PHŒBUS", None),
])
def test_chapter_numbering(heading, expected):
    assert ingest._chapter_number(heading) == expected


def test_split_at_breaks_on_paragraph_bounds(book):
    _, chapters = book
    chapter = ingest.select_chapter(chapters, "1.1")
    batches = ingest.split_paragraphs(chapter, split_at=8000)
    assert len(batches) > 1
    # No paragraph may be lost or split in half.
    assert sum(len(b) for b in batches) == len(chapter.paragraphs)
    assert [p for b in batches for p in b] == chapter.paragraphs
