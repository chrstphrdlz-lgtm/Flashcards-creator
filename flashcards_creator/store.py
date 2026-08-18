"""The persistent seen-words store.

This is what stops chapter 2 re-teaching chapter 1. Without it, a book-length
project re-cards the same vocabulary endlessly -- and it matters most for a
writer like Hugo, who reuses his architectural and medieval vocabulary across
the whole novel, so the first chapter absorbs the cost and later ones shrink.

Keyed by (lemma, POS) rather than lemma alone, because ``livre`` the noun and
``livrer`` the verb are genuinely different things to learn.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from . import paths


def _key(lemma: str, pos: str) -> str:
    return f"{lemma.lower()}|{pos}"


def load(path: Path | None = None) -> dict[str, Any]:
    """Read the store, returning {} if it does not exist yet."""
    path = path or paths.KNOWN_WORDS
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt store should not block deck building; worst case a few
        # words are carded twice.
        return {}
    return data.get("words", {}) if isinstance(data, dict) else {}


def save(words: dict[str, Any], path: Path | None = None) -> Path:
    path = path or paths.KNOWN_WORDS
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"updated": date.today().isoformat(), "count": len(words), "words": words}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def contains(words: dict[str, Any], lemma: str, pos: str) -> bool:
    return _key(lemma, pos) in words


def record(
    words: dict[str, Any],
    entries: Iterable[dict],
    book: str,
    chapter: str,
    source: str = "deck",
) -> int:
    """Mark words as seen. Existing entries keep their original first sighting."""
    added = 0
    today = date.today().isoformat()
    for entry in entries:
        key = _key(entry["lemma"], entry["pos"])
        if key in words:
            continue
        words[key] = {
            "lemma": entry["lemma"],
            "pos": entry["pos"],
            "level": entry.get("level"),
            "book": book,
            "chapter": chapter,
            "first_seen": today,
            "source": source,
        }
        added += 1
    return added


def forget(words: dict[str, Any], lemmas: Iterable[str]) -> int:
    """Drop words so they can be carded again."""
    removed = 0
    wanted = {lemma.lower() for lemma in lemmas}
    for key in [k for k, v in words.items() if v["lemma"].lower() in wanted]:
        del words[key]
        removed += 1
    return removed


def stats(words: dict[str, Any]) -> dict[str, Any]:
    by_level: dict[str, int] = {}
    by_chapter: dict[str, int] = {}
    for entry in words.values():
        level = entry.get("level") or "?"
        by_level[level] = by_level.get(level, 0) + 1
        chapter = f"{entry.get('book', '?')} {entry.get('chapter', '?')}"
        by_chapter[chapter] = by_chapter.get(chapter, 0) + 1
    return {"total": len(words), "by_level": by_level, "by_chapter": by_chapter}
