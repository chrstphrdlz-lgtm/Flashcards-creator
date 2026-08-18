"""Where everything lives on disk.

Two roots, both gitignored:

``data/``  reference lexicons, derived indexes and caches. Shared by every book.
``work/``  per-chapter pipeline artifacts and generated decks.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("FCC_DATA_DIR", REPO_ROOT / "data"))
WORK_DIR = Path(os.environ.get("FCC_WORK_DIR", REPO_ROOT / "work"))

# Raw downloads, kept so a re-run of fetch_data can skip the network.
RAW_DIR = DATA_DIR / "raw"

# Derived indexes built by scripts/fetch_data.py
FLELEX_INDEX = DATA_DIR / "flelex.json"
LEXIQUE_INDEX = DATA_DIR / "lexique.json"
WIKTIONARY_INDEX = DATA_DIR / "wiktionary.json"
FREQ_BANDS = DATA_DIR / "freq_bands.json"

# Caches and persistent state
AUDIO_CACHE = DATA_DIR / "audio"
LLM_CACHE = DATA_DIR / "llm_cache.json"
KNOWN_WORDS = DATA_DIR / "known_words.json"

CONFIG_FILE = REPO_ROOT / "flashcards.toml"
CREDITS_FILE = REPO_ROOT / "CREDITS.md"


def slugify(text: str) -> str:
    """Filesystem-safe slug, used for book directory names."""
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "book"


def chapter_dir(book_slug: str, chapter_id: str) -> Path:
    """Working directory for one chapter, e.g. work/notre-dame-de-paris/ch01.06."""
    return WORK_DIR / book_slug / f"ch{chapter_id}"


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, WORK_DIR, AUDIO_CACHE):
        d.mkdir(parents=True, exist_ok=True)
