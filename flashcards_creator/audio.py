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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

USER_AGENT = "FlashcardsCreator/0.1 (personal language-learning tool)"

# Wikimedia asks for a descriptive agent and reasonable pacing. Testing against
# the API without it produced HTTP 429s within a handful of requests.
#
# The interval is enforced globally across threads, so raising the worker count
# raises throughput without raising the request rate Wikimedia sees. At book
# scale this is the difference between ~90 minutes and ~20.
MIN_INTERVAL = 0.2
MAX_RETRIES = 4
DEFAULT_WORKERS = 5

COMMONS_API = "https://fr.wiktionary.org/w/api.php"

# Prefer French-from-France voices, then any French recording.
REGION_PREFERENCE = ("fra", "france", "paris")

_last_request = 0.0
_throttle_lock = threading.Lock()


@dataclass
class AudioResult:
    path: Path | None
    source: str        # "commons" | "wiktionary" | "tts" | ""
    credit: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


def _throttle() -> None:
    """Space out requests globally, however many threads are running.

    Holding the lock across the sleep makes the rate a property of the process
    rather than of each thread: five workers still issue requests one per
    MIN_INTERVAL between them, they just spend the wait doing useful work.
    """
    global _last_request
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _safe_name(lemma: str) -> str:
    return re.sub(r"[^\w\-]", "_", lemma, flags=re.UNICODE)


def cached_path(lemma: str) -> Path:
    return paths.AUDIO_CACHE / f"{_safe_name(lemma)}.mp3"


def tts_path(lemma: str) -> Path:
    """Synthetic audio lives under its own name.

    Keeping it separate from the native cache matters: if a real recording is
    fetched later, it lands on the native path and immediately takes priority,
    so a deck built with synthetic audio upgrades itself on a rebuild instead
    of being stuck with the robot voice forever.
    """
    return paths.AUDIO_CACHE / f"{_safe_name(lemma)}.tts.mp3"


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
    allow_download: bool = True,
) -> AudioResult:
    """Get pronunciation audio for one word, preferring a real human voice.

    Order: cached native, downloaded native, cached synthetic, new synthetic.
    Native always wins, so a cache that fills up over time keeps improving the
    decks built from it.

    ``allow_download`` exists because Wikimedia throttles hard by IP. Measured
    here, roughly 83% of requests came back 429 and the rate was unaffected by
    slowing down from 5 requests/second to 1.4 -- so on a throttled address the
    only useful setting is off, using whatever is already cached and
    synthesising the rest.
    """
    dest = cached_path(lemma)
    if dest.exists() and dest.stat().st_size > 0:
        return AudioResult(dest, "cache", credit)

    if allow_download:
        if index_url and _fetch(index_url, dest):
            return AudioResult(dest, "commons", credit)

        if allow_live:
            url, name = _from_french_wiktionary(lemma)
            if url and _fetch(url, dest):
                return AudioResult(dest, "wiktionary", name)

    if allow_tts:
        spoken = tts_path(lemma)
        if spoken.exists() and spoken.stat().st_size > 0:
            return AudioResult(spoken, "tts", "synthesised (gTTS)")
        if _synthesise(lemma, spoken):
            return AudioResult(spoken, "tts", "synthesised (gTTS)")

    return AudioResult(None, "")


def fetch_many(
    rows: list[dict],
    allow_live: bool = True,
    allow_tts: bool = False,
    workers: int = DEFAULT_WORKERS,
    allow_download: bool = True,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Fetch audio for many words at once, writing paths back onto the rows.

    Downloads are all network wait, so threads help even though the rate limit
    is global. Cached words never touch the network, which makes an interrupted
    run cheap to resume.

    Returns how many words ended up with audio.
    """
    say = progress or (lambda _msg: None)
    total = len(rows)
    done = got = 0
    lock = threading.Lock()

    def work(row: dict) -> None:
        nonlocal done, got
        result = fetch_audio(
            row["lemma"], index_url=row.get("audio_url", ""),
            credit=row.get("audio_credit", ""),
            allow_live=allow_live, allow_tts=allow_tts,
            allow_download=allow_download)
        with lock:
            done += 1
            if result.ok:
                got += 1
                row["audio_path"] = str(result.path)
                row["credits"] = credit_line(row["lemma"], result)
            else:
                row["audio_path"] = ""
                row["credits"] = ""
            if done % 250 == 0 or done == total:
                say(f"  audio {done}/{total} ({got} found)")

    if workers <= 1:
        for row in rows:
            work(row)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, rows))
    return got


def credit_line(lemma: str, result: AudioResult) -> str:
    """Attribution for the card. Commons recordings are CC BY-SA."""
    if not result.ok:
        return ""
    if result.source == "tts":
        return "synthesised speech (gTTS) — not a native recording"
    if result.credit:
        return f"audio: {result.credit} (Wikimedia Commons, CC BY-SA)"
    return "audio: Wikimedia Commons (CC BY-SA)"
