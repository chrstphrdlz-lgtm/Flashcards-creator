"""Stage 5: build the .apkg.

Cards are grouped by paragraph using **tags** and a zero-padded sort field
rather than subdecks -- a chapter can run to 139 paragraphs, and 139 subdecks
would be unusable.

Each deck is stamped with its chapter's opening line in two places, because
they are visible at different moments in Anki: the deck description shows on
the overview screen when you tap in, and the incipit card is the first thing
the deck actually shows you. Deck descriptions are easy to skim past and some
clients truncate them; a first card is not missable.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import genanki

CSS = """
.card {
  font-family: Georgia, 'Iowan Old Style', serif;
  font-size: 20px;
  text-align: center;
  color: #1a1a1a;
  background: #fbfaf7;
  padding: 16px;
}
.word { font-size: 34px; font-weight: 600; margin-bottom: 4px; }
.pos { font-size: 15px; color: #6b6357; font-style: italic; }
.ipa { font-size: 16px; color: #6b6357; font-family: 'Charis SIL', serif; }
.translation { font-size: 26px; color: #14532d; margin: 14px 0; }
.note { font-size: 16px; color: #5b5347; font-style: italic; margin-bottom: 10px; }
.sentence {
  font-size: 17px; color: #33302b; text-align: left;
  margin: 14px auto 0; max-width: 34em; line-height: 1.55;
  border-left: 3px solid #d8d2c4; padding-left: 12px;
}
.sentence b { color: #14532d; }
.meta { font-size: 13px; color: #8a8274; margin-top: 14px; }
.credit { font-size: 11px; color: #a8a094; margin-top: 8px; }
hr { border: none; border-top: 1px solid #e3ddd0; margin: 14px 0; }
.incipit {
  font-size: 19px; text-align: left; line-height: 1.6;
  max-width: 34em; margin: 0 auto; font-style: italic;
}
.chapter { font-size: 26px; font-weight: 600; margin-bottom: 6px; }
.book { font-size: 15px; color: #6b6357; }
"""

VOCAB_MODEL_ID = 1607392913
INCIPIT_MODEL_ID = 1607392914

VOCAB_MODEL = genanki.Model(
    VOCAB_MODEL_ID,
    "FR Reading Vocab",
    fields=[
        {"name": "Sort"}, {"name": "Word"}, {"name": "Display"},
        {"name": "POS"}, {"name": "IPA"}, {"name": "Audio"},
        {"name": "Translation"}, {"name": "Note"}, {"name": "Sentence"},
        {"name": "Level"}, {"name": "LevelSource"}, {"name": "Paragraph"},
        {"name": "Credits"},
    ],
    templates=[{
        "name": "Recognition",
        "qfmt": """
<div class="word">{{Display}}</div>
<div class="pos">{{POS}}</div>
{{#IPA}}<div class="ipa">{{IPA}}</div>{{/IPA}}
{{#Audio}}<div>{{Audio}}</div>{{/Audio}}
""",
        "afmt": """
<div class="word">{{Display}}</div>
<div class="pos">{{POS}}</div>
{{#IPA}}<div class="ipa">{{IPA}}</div>{{/IPA}}
{{#Audio}}<div>{{Audio}}</div>{{/Audio}}
<hr>
<div class="translation">{{Translation}}</div>
{{#Note}}<div class="note">{{Note}}</div>{{/Note}}
{{#Sentence}}<div class="sentence">{{Sentence}}</div>{{/Sentence}}
<div class="meta">{{Level}} &middot; {{LevelSource}} &middot; &para;{{Paragraph}}</div>
{{#Credits}}<div class="credit">{{Credits}}</div>{{/Credits}}
""",
    }],
    css=CSS,
    sort_field_index=0,
)

INCIPIT_MODEL = genanki.Model(
    INCIPIT_MODEL_ID,
    "FR Chapter Incipit",
    fields=[{"name": "Sort"}, {"name": "Chapter"}, {"name": "Book"},
            {"name": "Opening"}, {"name": "Summary"}],
    templates=[{
        "name": "Chapter card",
        "qfmt": """
<div class="chapter">{{Chapter}}</div>
<div class="book">{{Book}}</div>
<hr>
<div class="pos">This deck's chapter opens…</div>
""",
        "afmt": """
<div class="chapter">{{Chapter}}</div>
<div class="book">{{Book}}</div>
<hr>
<div class="incipit">{{Opening}}</div>
<div class="meta">{{Summary}}</div>
""",
    }],
    css=CSS,
    sort_field_index=0,
)


class DeckError(Exception):
    """Raised when a deck cannot be assembled."""


@dataclass
class DeckMeta:
    book: str
    chapter_label: str
    chapter_id: str
    incipit: str
    paragraphs: int


def _stable_id(text: str) -> int:
    """A deck id derived from its name, so rebuilding updates in place.

    A random id would create a second deck on every rebuild rather than
    replacing the first.
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return 1 << 30 | int(digest, 16) % (1 << 30)


def deck_name(book: str, chapter_id: str) -> str:
    safe_book = book.replace("::", "-")
    return f"French::{safe_book}::Ch {chapter_id}"


def build_description(meta: DeckMeta, card_count: int, threshold: str) -> str:
    """The deck's overview text: which chapter this is, and how it opens."""
    lines = [
        f"<b>{html.escape(meta.chapter_label)}</b>",
        f"<i>Commence :</i> « {html.escape(meta.incipit)} »",
        f"{card_count} cards &middot; {threshold}+ &middot; "
        f"{html.escape(meta.book)} &middot; built {date.today().isoformat()}",
    ]
    return "<br>".join(lines)


def highlight(sentence: str, surfaces: Iterable[str]) -> str:
    """Bold the word being learned inside its sentence."""
    escaped = html.escape(sentence)
    for surface in sorted(set(surfaces), key=len, reverse=True):
        if not surface:
            continue
        pattern = re.compile(rf"\b({re.escape(html.escape(surface))})\b")
        new, count = pattern.subn(r"<b>\1</b>", escaped, count=1)
        if count:
            return new
    return escaped


def build_deck(
    rows: list[dict],
    meta: DeckMeta,
    output: Path,
    threshold: str = "B2",
    incipit_card: bool = True,
) -> dict[str, Any]:
    """Assemble a one-chapter .apkg. Returns a summary of what went into it."""
    anki_deck, media, count = compose_deck(rows, meta, threshold, incipit_card)

    output.parent.mkdir(parents=True, exist_ok=True)
    package = genanki.Package(anki_deck)
    package.media_files = media
    package.write_to_file(str(output))

    return {
        "deck": anki_deck.name,
        "path": str(output),
        "cards": count,
        "incipit_card": incipit_card,
        "media_files": len(media),
        "paragraphs": len({r.get("first_paragraph") for r in rows}),
    }


def build_book(
    chapters: list[tuple[list[dict], DeckMeta]],
    output: Path,
    threshold: str = "B2",
    incipit_card: bool = True,
) -> dict[str, Any]:
    """Pack every chapter into one .apkg as a tree of subdecks.

    genanki.Package takes a list of decks, and Anki builds the hierarchy from
    the `::` in each name -- so 59 chapters import in a single step and land
    under one parent rather than as 59 separate files to shepherd.
    """
    if not chapters:
        raise DeckError("no chapters to build")

    decks, media, total = [], [], 0
    for rows, meta in chapters:
        if not rows:
            continue
        anki_deck, chapter_media, count = compose_deck(
            rows, meta, threshold, incipit_card)
        decks.append(anki_deck)
        media.extend(chapter_media)
        total += count

    if not decks:
        raise DeckError("every chapter was empty after selection")

    output.parent.mkdir(parents=True, exist_ok=True)
    package = genanki.Package(decks)
    # One file may be shared by several chapters only if a word repeated, which
    # the seen-store prevents -- but dedupe anyway so the archive stays clean.
    package.media_files = sorted(set(media))
    package.write_to_file(str(output))

    return {
        "path": str(output),
        "decks": len(decks),
        "cards": total,
        "media_files": len(package.media_files),
    }


def compose_deck(
    rows: list[dict],
    meta: DeckMeta,
    threshold: str = "B2",
    incipit_card: bool = True,
) -> tuple[genanki.Deck, list[str], int]:
    """Build one chapter's deck in memory, without writing it.

    Split out from build_deck so a whole book can be packaged in one archive.
    """
    if not rows:
        raise DeckError(
            "no words to build a deck from. Either the chapter had nothing "
            f"at {threshold} or above, or selection dropped everything."
        )

    name = deck_name(meta.book, meta.chapter_id)
    anki_deck = genanki.Deck(
        _stable_id(name), name,
        description=build_description(meta, len(rows), threshold),
    )

    media: list[str] = []
    chapter_tag = f"chapter-{meta.chapter_id.replace('.', '-')}"

    if incipit_card:
        anki_deck.add_note(genanki.Note(
            model=INCIPIT_MODEL,
            fields=[
                # Same shape as the vocabulary sort keys below. A bare "0000"
                # would land in Anki's integer-affinity sort column as the
                # number 0, leaving the order to an implicit int-before-text
                # rule rather than to the key itself.
                "0000-0000",
                html.escape(meta.chapter_label),
                html.escape(meta.book),
                f"« {html.escape(meta.incipit)} »",
                f"{len(rows)} cards &middot; {meta.paragraphs} paragraphs",
            ],
            tags=["incipit", chapter_tag],
            guid=genanki.guid_for(name, "incipit"),
        ))

    # Paragraph order is reading order, which is how the chapter will be read.
    ordered = sorted(rows, key=lambda r: (r.get("first_paragraph", 0), r["lemma"]))

    for position, row in enumerate(ordered, start=1):
        para = int(row.get("first_paragraph") or 0)
        audio_field = ""
        audio_path = row.get("audio_path")
        if audio_path:
            path = Path(audio_path)
            if path.exists():
                media.append(str(path))
                audio_field = f"[sound:{path.name}]"

        tags = [
            chapter_tag,
            f"para-{para:03d}",
            f"level-{row.get('level', 'NA')}",
            f"src-{row.get('level_source', 'na')}",
        ]

        anki_deck.add_note(genanki.Note(
            model=VOCAB_MODEL,
            fields=[
                f"{para:04d}-{position:04d}",
                row["lemma"],
                row.get("display") or row["lemma"],
                row.get("pos_label") or row.get("pos", ""),
                row.get("ipa", ""),
                audio_field,
                row.get("translation", ""),
                row.get("note", ""),
                highlight(row.get("sentence", ""), row.get("surfaces") or []),
                row.get("level", ""),
                row.get("level_source", ""),
                str(para),
                row.get("credits", ""),
            ],
            # Stable across rebuilds, so Anki updates a card instead of
            # duplicating it and losing its review history.
            guid=genanki.guid_for(name, row["lemma"], row.get("pos", "")),
            tags=tags,
        ))

    return anki_deck, media, len(ordered)
