#!/usr/bin/env python3
"""Download the reference lexicons and build the compact indexes.

Run once before the first chapter:

    python scripts/fetch_data.py

Sources, all fetched at setup rather than committed (see CREDITS.md for
licences -- FLELex in particular is CC BY-NC-SA):

  FLELex-Beacco   CEFR level per lemma, from FFL textbooks + Beacco referentials
  Lexique 3.83    inflected form -> lemma, POS, and modern book frequency
  Kaikki          French Wiktionary: English glosses, gender, IPA, audio URLs

The Kaikki dump is ~573MB. It is streamed and reduced to a compact index; pass
--keep-raw if you would rather not re-download it on a rebuild.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flashcards_creator import paths  # noqa: E402

FLELEX_URL = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_TT_Beacco.tsv"
LEXIQUE_URL = "http://www.lexique.org/databases/Lexique383/Lexique383.tsv"
KAIKKI_URL = "https://kaikki.org/dictionary/French/kaikki.org-dictionary-French.jsonl"

USER_AGENT = "FlashcardsCreator/0.1 (personal language-learning tool)"

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Wiktionary POS names -> the spaCy UPOS tags we look words up by.
WIKT_POS_MAP = {
    "noun": "NOUN",
    "verb": "VERB",
    "adj": "ADJ",
    "adv": "ADV",
    "intj": "INTJ",
    "num": "NUM",
    "pron": "PRON",
    "prep": "ADP",
    "conj": "CCONJ",
    "name": "PROPN",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path, attempts: int = 5) -> Path:
    """Fetch a URL to disk, retrying with exponential backoff.

    The FLELex host in particular resets connections intermittently, so this
    retries rather than failing the whole setup on a flaky moment.
    """
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  already have {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                nxt = 10 * 1024 * 1024
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if done >= nxt:
                        pct = f" ({done * 100 // total}%)" if total else ""
                        log(f"    {done / 1e6:,.0f} MB{pct}")
                        nxt += 10 * 1024 * 1024
            tmp.replace(dest)
            log(f"  downloaded {dest.name} ({dest.stat().st_size:,} bytes)")
            return dest
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            tmp.unlink(missing_ok=True)
            if attempt < attempts:
                wait = 2**attempt
                log(f"  attempt {attempt} failed ({exc}); retrying in {wait}s")
                time.sleep(wait)

    raise RuntimeError(f"could not download {url} after {attempts} attempts: {last}")


# --------------------------------------------------------------------------
# FLELex: the authoritative CEFR layer
# --------------------------------------------------------------------------

def build_flelex(raw: Path) -> dict:
    """lemma -> {treetagger_tag: level}, plus each tag's total frequency.

    The frequency is kept so tier 2 (lemma match ignoring POS) can pick the
    dominant reading rather than an arbitrary one.
    """
    index: dict[str, dict[str, dict]] = defaultdict(dict)
    with raw.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            word = (row.get("word") or "").strip().lower()
            tag = (row.get("tag") or "").strip()
            level = (row.get("level") or "").strip()
            if not word or level not in LEVELS:
                continue
            try:
                freq = float(row.get("freq_total") or 0.0)
            except ValueError:
                freq = 0.0
            index[word][tag] = {"level": level, "freq": freq}

    log(f"  FLELex: {len(index):,} lemmas")
    counts: dict[str, int] = defaultdict(int)
    for tags in index.values():
        for entry in tags.values():
            counts[entry["level"]] += 1
    log("  levels: " + "  ".join(f"{lv}:{counts[lv]:,}" for lv in LEVELS))
    return dict(index)


# --------------------------------------------------------------------------
# Lexique: lemmatisation fallback + the modern-frequency signal
# --------------------------------------------------------------------------

def build_lexique(raw: Path) -> dict:
    """Two maps: surface form -> lemma, and lemma -> (POS, modern book freq).

    ``freqlemlivres`` is occurrences per million in a corpus of books. It is the
    signal that separates genuinely useful advanced vocabulary from words that
    are merely archaic -- both of which land at C1/C2.
    """
    forms: dict[str, str] = {}
    lemmas: dict[str, dict] = {}

    with raw.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            ortho = (row.get("ortho") or "").strip().lower()
            lemme = (row.get("lemme") or "").strip().lower()
            cgram = (row.get("cgram") or "").strip()
            if not ortho or not lemme:
                continue
            forms.setdefault(ortho, lemme)

            if row.get("islem") == "1" and lemme not in lemmas:
                try:
                    books = float(row.get("freqlemlivres") or 0.0)
                except ValueError:
                    books = 0.0
                try:
                    films = float(row.get("freqlemfilms2") or 0.0)
                except ValueError:
                    films = 0.0
                lemmas[lemme] = {"pos": cgram, "books": books, "films": films}

    log(f"  Lexique: {len(forms):,} surface forms, {len(lemmas):,} lemmas")
    return {"forms": forms, "lemmas": lemmas}


# --------------------------------------------------------------------------
# Tier 3 calibration: derive frequency bands from FLELex rather than guess them
# --------------------------------------------------------------------------

def calibrate_bands(flelex: dict, lexique: dict, off_list_floor: str = "B2") -> dict:
    """Learn a frequency->CEFR mapping from words whose level we already know.

    Deliberately coarse, because the data says it has to be. Measured on FLELex,
    the per-level frequency distributions overlap almost completely in the
    middle of the range: A2 and B1 have near-identical medians (21.35 vs 21.82
    per million) and C2 is *more* frequent than C1 (1.92 vs 1.58). Frequency
    genuinely cannot separate those pairs, so claiming six-level precision here
    would be false confidence.

    What it can do is place a word coarsely. We bin FLELex lemmas by log
    frequency, take the weighted median level of each bin, and enforce
    monotonicity across bins. Each bin also records how confident that call is,
    which stage 2 passes through to the card as ``level_confidence``.

    Off-list words get a floor (default B2): FLELex covers the core vocabulary
    of French-as-a-foreign-language teaching, so a word missing from it is by
    construction outside the standard curriculum. The bin data supports this --
    below 30 occurrences per million, B2-or-above accounts for 76%+ of FLELex
    words, and off-list words skew harder still at the same frequency.
    """
    lemma_freq = lexique["lemmas"]
    samples: list[tuple[float, int]] = []  # (frequency, level index)
    per_level: dict[str, list[float]] = {lv: [] for lv in LEVELS}

    for word, tags in flelex.items():
        info = lemma_freq.get(word)
        if not info or info["books"] <= 0:
            continue
        # A lemma can carry several tags; use its most frequent reading.
        best = max(tags.values(), key=lambda e: e["freq"])
        samples.append((info["books"], LEVELS.index(best["level"])))
        per_level[best["level"]].append(info["books"])

    # Log-spaced edges spanning the useful range, plus open ends.
    edges = [0.0] + [round(10 ** (i / 4), 4) for i in range(-8, 13)] + [math.inf]

    raw_bins: list[dict] = []
    for lo, hi in zip(edges, edges[1:]):
        levels = [lv for freq, lv in samples if lo <= freq < hi]
        if not levels:
            continue
        levels.sort()
        median_idx = levels[len(levels) // 2]
        counts = [levels.count(i) for i in range(len(LEVELS))]
        n = len(levels)
        raw_bins.append({
            "min_freq": lo,
            "max_freq": None if hi == math.inf else hi,
            "n": n,
            "level_idx": median_idx,
            "agreement": round(counts[median_idx] / n, 3),
            "share_b2_plus": round(sum(counts[LEVELS.index("B2"):]) / n, 3),
        })

    # Merge thin bins into their neighbour so a handful of words cannot swing a
    # band, then enforce: difficulty never falls as frequency rises.
    merged: list[dict] = []
    for b in raw_bins:
        if merged and b["n"] < 100:
            prev = merged[-1]
            total = prev["n"] + b["n"]
            prev["max_freq"] = b["max_freq"]
            prev["agreement"] = round(
                (prev["agreement"] * prev["n"] + b["agreement"] * b["n"]) / total, 3)
            prev["share_b2_plus"] = round(
                (prev["share_b2_plus"] * prev["n"] + b["share_b2_plus"] * b["n"]) / total, 3)
            prev["n"] = total
        else:
            merged.append(dict(b))

    ceiling = len(LEVELS) - 1
    for b in merged:  # ascending frequency => level index must not increase
        b["level_idx"] = min(b["level_idx"], ceiling)
        ceiling = b["level_idx"]

    bins = [{
        "min_freq": b["min_freq"],
        "max_freq": b["max_freq"],
        "level": LEVELS[b["level_idx"]],
        "n": b["n"],
        "agreement": b["agreement"],
        "share_b2_plus": b["share_b2_plus"],
    } for b in merged]

    bands = {
        "note": (
            "Derived from FLELex: weighted-median CEFR level per log-frequency "
            "bin of Lexique freqlemlivres (per million, books), monotonically "
            "smoothed. Coarse by design -- frequency cannot separate A2 from B1 "
            "or C1 from C2. Rebuild with scripts/fetch_data.py."
        ),
        "off_list_floor": off_list_floor,
        "off_list_floor_note": (
            "Words absent from FLELex are usually outside the standard FFL "
            "curriculum, so their estimated level is not reported below this "
            "floor -- unless they are common enough that FLELex is simply "
            "missing them, which off_list_floor_max_freq decides."
        ),
        # Above this many occurrences per million, the calibration bins put
        # A1-B1 at 76%+ of FLELex words, so an off-list word here is far more
        # likely omitted basic vocabulary than advanced vocabulary.
        "off_list_floor_max_freq": 30.0,
        "medians": {lv: round(statistics.median(v), 2)
                    for lv, v in per_level.items() if v},
        "sample_sizes": {lv: len(v) for lv, v in per_level.items()},
        "bins": bins,
    }

    log(f"  calibrated {len(bins)} frequency bins from {len(samples):,} FLELex lemmas")
    log(f"  off-list floor: {off_list_floor}")
    for b in bins:
        hi = "inf" if b["max_freq"] is None else f"{b['max_freq']:g}"
        log(f"    [{b['min_freq']:>8g}, {hi:>8})  ->  {b['level']}   "
            f"n={b['n']:<6,} agreement={b['agreement']:.0%}")
    return bands


# --------------------------------------------------------------------------
# Kaikki: glosses, gender, IPA and audio in one streamed pass
# --------------------------------------------------------------------------

def build_wiktionary(raw: Path, max_glosses: int = 4) -> dict:
    """word -> {UPOS: {glosses, gender, ipa, audio}}.

    Streamed line by line because the dump is ~573MB. We keep only what a card
    needs, which shrinks it by well over an order of magnitude.

    Audio uses ``mp3_url`` rather than ``ogg_url`` -- Anki plays MP3 natively
    and Wikimedia already serves an MP3 transcode, so no ffmpeg is needed.
    Recordings tagged as French-from-France are preferred over other regions.
    """
    index: dict[str, dict[str, dict]] = defaultdict(dict)
    opener = gzip.open if raw.suffix == ".gz" else open

    seen = 0
    with opener(raw, "rt", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            seen += 1
            if seen % 250_000 == 0:
                log(f"    {seen:,} entries, {len(index):,} words")

            word = (rec.get("word") or "").strip()
            if not word or rec.get("lang_code") != "fr":
                continue
            upos = WIKT_POS_MAP.get(rec.get("pos") or "")
            if not upos:
                continue

            glosses: list[str] = []
            gender = ""
            for sense in rec.get("senses") or []:
                tags = sense.get("tags") or []
                # Form-of entries ("plural of X") add noise, not meaning.
                if "form-of" in tags or sense.get("form_of"):
                    continue
                for gloss in sense.get("glosses") or []:
                    gloss = gloss.strip()
                    if gloss and gloss not in glosses:
                        glosses.append(gloss)
                if not gender:
                    if "masculine" in tags:
                        gender = "m"
                    elif "feminine" in tags:
                        gender = "f"

            if not gender:
                head = " ".join(rec.get("head_templates_text") or [])
                if "masculine" in head:
                    gender = "m"
                elif "feminine" in head:
                    gender = "f"

            ipa = ""
            audio = ""
            audio_credit = ""
            best_rank = 99
            for sound in rec.get("sounds") or []:
                if not ipa and sound.get("ipa"):
                    ipa = sound["ipa"]
                url = sound.get("mp3_url")
                if not url:
                    continue
                tags = [t.lower() for t in (sound.get("tags") or [])]
                if "france" in tags or "paris" in tags:
                    rank = 0
                elif tags:
                    rank = 1
                else:
                    rank = 2
                if rank < best_rank:
                    best_rank, audio = rank, url
                    audio_credit = sound.get("audio") or ""

            if not (glosses or audio or ipa):
                continue

            entry = index[word].get(upos)
            if entry is None:
                index[word][upos] = {
                    "glosses": glosses[:max_glosses],
                    "gender": gender,
                    "ipa": ipa,
                    "audio": audio,
                    "audio_credit": audio_credit,
                }
            else:
                # Wiktionary splits some words across etymologies; merge them.
                for gloss in glosses:
                    if len(entry["glosses"]) < max_glosses and gloss not in entry["glosses"]:
                        entry["glosses"].append(gloss)
                entry["gender"] = entry["gender"] or gender
                entry["ipa"] = entry["ipa"] or ipa
                if not entry["audio"] and audio:
                    entry["audio"] = audio
                    entry["audio_credit"] = audio_credit

    log(f"  Wiktionary: {len(index):,} words from {seen:,} entries")
    with_audio = sum(1 for v in index.values() if any(e["audio"] for e in v.values()))
    log(f"  with audio: {with_audio:,} ({with_audio * 100 // max(len(index), 1)}%)")
    return dict(index)


def save(obj: dict, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    log(f"  wrote {dest.name} ({dest.stat().st_size / 1e6:,.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the raw downloads (the Kaikki dump is ~573MB)")
    ap.add_argument("--skip-wiktionary", action="store_true",
                    help="skip the big dump; decks build without glosses or audio")
    ap.add_argument("--force", action="store_true",
                    help="rebuild indexes even if they already exist")
    args = ap.parse_args()

    paths.ensure_dirs()

    log("FLELex-Beacco (CEFR levels)")
    flelex_raw = download(FLELEX_URL, paths.RAW_DIR / "FleLex_TT_Beacco.tsv")
    flelex = build_flelex(flelex_raw)
    save(flelex, paths.FLELEX_INDEX)

    log("\nLexique 3.83 (lemmas + modern frequency)")
    lexique_raw = download(LEXIQUE_URL, paths.RAW_DIR / "Lexique383.tsv")
    lexique = build_lexique(lexique_raw)
    save(lexique, paths.LEXIQUE_INDEX)

    log("\nCalibrating tier-3 frequency bands")
    save(calibrate_bands(flelex, lexique), paths.FREQ_BANDS)

    if args.skip_wiktionary:
        log("\nSkipping Wiktionary (--skip-wiktionary)")
    elif paths.WIKTIONARY_INDEX.exists() and not args.force:
        log(f"\nWiktionary index already built ({paths.WIKTIONARY_INDEX.name}); "
            "pass --force to rebuild")
    else:
        log("\nKaikki French Wiktionary (~573MB, streamed)")
        kaikki_raw = download(KAIKKI_URL, paths.RAW_DIR / "kaikki-fr.jsonl")
        save(build_wiktionary(kaikki_raw), paths.WIKTIONARY_INDEX)
        if not args.keep_raw:
            kaikki_raw.unlink(missing_ok=True)
            log("  removed the raw dump (pass --keep-raw to keep it)")

    if not args.keep_raw:
        shutil.rmtree(paths.RAW_DIR, ignore_errors=True)

    log("\nDone. Reference data is in " + str(paths.DATA_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
