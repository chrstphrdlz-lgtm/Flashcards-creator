"""Pronunciation audio for cards.

Primary source is Wikimedia Commons, mostly Lingua Libre recordings by native
speakers, whose URLs already sit in the local Wiktionary index -- so finding
audio costs no network at all. Only the download itself hits the network, and
each file is cached, so later chapters reuse whatever earlier ones fetched.

Always MP3, never Ogg: Wikimedia serves ready-made MP3 transcodes, Anki plays
MP3 natively, and this avoids any dependency on ffmpeg.

Two fallbacks for words the index has no recording for -- a live query against
the French Wiktionary, whose French coverage is better than the English one's,
and finally synthetic text-to-speech, which is always labelled as such on the
card so a robot voice is never mistaken for a native speaker.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths

USER_AGENT = "FlashcardsCreator/0.1 (personal language-learning tool)"

# Wikimedia asks for a descriptive agent and reasonable pacing. Testing against
# the API without it produced HTTP 429s within a handful of requests.
MIN_INTERVAL = 1.0
MAX_RETRIES = 4

COMMONS_API = "https://fr.wiktionary.org/w/api.php"

# Prefer French-from-France voices, then any French recording.
REGION_PREFERENCE = ("fra", "france", "paris")

_last_request = 0.0


@dataclass
class AudioResult:
    path: Path | None
    source: str        # "commons" | "wiktionary" | "tts" | ""
    credit: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


def _throttle() -> None:
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _safe_name(lemma: str) -> str:
    return re.sub(r"[^\w\-]", "_", lemma, flags=re.UNICODE)


def cached_path(lemma: str) -> Path:
    return paths.AUDIO_CACHE / f"{_safe_name(lemma)}.mp3"


def _fetch(url: str, dest: Path) -> bool:
    """Download one file, backing off on rate limits."""
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
            if not data:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            return False
        except (urllib.error.URLError, OSError, TimeoutError):
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            return False
    return False


def _from_french_wiktionary(lemma: str) -> tuple[str, str]:
    """Ask fr.wiktionary for a recording. Returns (mp3 url, credit)."""
    params = {
        "action": "query", "titles": lemma, "prop": "images",
        "imlimit": "50", "format": "json",
    }
    _throttle()
    try:
        req = urllib.request.Request(
            f"{COMMONS_API}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return "", ""

    files = [
        img["title"][len("Fichier:"):]
        for page in data.get("query", {}).get("pages", {}).values()
        for img in page.get("images", [])
        if img.get("title", "").startswith("Fichier:")
        and img["title"].lower().endswith((".ogg", ".wav", ".mp3", ".flac"))
    ]
    if not files:
        return "", ""

    def rank(name: str) -> int:
        low = name.lower()
        if not re.search(rf"[-_ ]{re.escape(lemma.lower())}\.", low):
            return 3  # a recording of some other word on the same page
        for i, region in enumerate(REGION_PREFERENCE):
            if region in low:
                return i
        return len(REGION_PREFERENCE)

    best = min(files, key=rank)
    if rank(best) >= 3:
        return "", ""

    # Commons serves an MP3 transcode of every audio file at a predictable path.
    encoded = urllib.parse.quote(best.replace(" ", "_"))
    url = ("https://upload.wikimedia.org/wikipedia/commons/transcoded/"
           f"{_commons_hash_path(best)}/{encoded}/{encoded}.mp3")
    return url, best


def _commons_hash_path(filename: str) -> str:
    """Commons stores files under md5(name)[0]/md5(name)[0:2]."""
    import hashlib

    digest = hashlib.md5(filename.replace(" ", "_").encode("utf-8")).hexdigest()
    return f"{digest[0]}/{digest[:2]}"


def _synthesise(lemma: str, dest: Path) -> bool:
    try:
        from gtts import gTTS
    except ImportError:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        gTTS(text=lemma, lang="fr").save(str(dest))
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        # Network or upstream failure; a card without audio is fine.
        return False


def fetch_audio(
    lemma: str,
    index_url: str = "",
    credit: str = "",
    allow_live: bool = True,
    allow_tts: bool = False,
) -> AudioResult:
    """Get pronunciation audio for one word, using the cache when possible."""
    dest = cached_path(lemma)
    if dest.exists() and dest.stat().st_size > 0:
        return AudioResult(dest, "cache", credit)

    if index_url and _fetch(index_url, dest):
        return AudioResult(dest, "commons", credit)

    if allow_live:
        url, name = _from_french_wiktionary(lemma)
        if url and _fetch(url, dest):
            return AudioResult(dest, "wiktionary", name)

    if allow_tts and _synthesise(lemma, dest):
        return AudioResult(dest, "tts", "synthesised (gTTS)")

    return AudioResult(None, "")


def credit_line(lemma: str, result: AudioResult) -> str:
    """Attribution for the card. Commons recordings are CC BY-SA."""
    if not result.ok:
        return ""
    if result.source == "tts":
        return "synthesised speech (gTTS) — not a native recording"
    if result.credit:
        return f"audio: {result.credit} (Wikimedia Commons, CC BY-SA)"
    return "audio: Wikimedia Commons (CC BY-SA)"
