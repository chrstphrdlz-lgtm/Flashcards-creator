"""Command line entry point: one subcommand per pipeline stage.

    fcc chapters  --epub B.epub                 list the chapters
    fcc extract   --epub B.epub --chapter 1.6   EPUB      -> words.json
    fcc level     --chapter 1.6                 words     -> words_leveled.json
    fcc enrich    --chapter 1.6                 selection -> words_enriched.json
    fcc build     --chapter 1.6                 enriched  -> .apkg

Selection sits between `level` and `enrich` and has no subcommand: it is where
you say what you want kept, and the french-flashcards skill drives it. Running
`enrich` without a selection.json falls back to the mechanical default, so the
pipeline still works end to end on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import (artifacts, audio, config, deck, glosses, ingest, leveling,
               lexicon, llm, nlp, paths, store, triage)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _book_slug(title: str) -> str:
    return paths.slugify(title)


def _normalise_spec(spec: str) -> str:
    """'1.6' -> '01.06', '6' -> '06'. Matches Chapter.chapter_id's format."""
    spec = str(spec).strip()
    try:
        if "." in spec:
            part, number = spec.split(".", 1)
            return f"{int(part):02d}.{int(number):02d}"
        return f"{int(spec):02d}"
    except ValueError:
        return spec


def _resolve_dir(args, cfg) -> tuple[str, str, Path]:
    """Find the chapter directory `extract` created for this chapter.

    Stages after `extract` do not open the EPUB, so they cannot re-derive a
    chapter id from a flat position. They locate the directory instead: by the
    normalised id, and failing that by the chapter index recorded in the
    artifact -- which is what makes `--chapter 6` and `--chapter 1.6` work
    interchangeably all the way down the pipeline.
    """
    book = args.book or cfg.get("book")
    if not book:
        raise SystemExit(
            "error: which book? Pass --book, or set book in flashcards.toml")
    slug = _book_slug(book)

    direct = paths.chapter_dir(slug, _normalise_spec(args.chapter))
    if direct.exists():
        return book, direct.name[2:], direct

    root = paths.WORK_DIR / slug
    if root.exists():
        wanted = str(args.chapter).strip()
        for candidate in sorted(root.glob("ch*")):
            meta = candidate / "paragraphs.json"
            if not meta.exists():
                continue
            try:
                doc = json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if wanted.isdigit() and doc.get("chapter_index") == int(wanted):
                return book, candidate.name[2:], candidate
            if doc.get("chapter_id") == _normalise_spec(wanted):
                return book, candidate.name[2:], candidate

    available = sorted(p.name[2:] for p in (paths.WORK_DIR / slug).glob("ch*")) \
        if root.exists() else []
    hint = (f" Extracted so far: {', '.join(available)}." if available
            else f" Run `fcc extract --chapter {args.chapter}` first.")
    raise SystemExit(f"error: no extracted chapter {args.chapter} for {book!r}.{hint}")


# ---------------------------------------------------------------------------
# fcc chapters
# ---------------------------------------------------------------------------

def cmd_chapters(args, cfg) -> int:
    epub = args.epub or cfg.get("epub")
    if not epub:
        return _fail("no EPUB given; pass --epub or set epub in flashcards.toml")

    title, chapters = ingest.read_chapters(Path(epub))
    print(f"{title} — {len(chapters)} chapters\n")

    current_part = object()
    for ch in chapters:
        if ch.part != current_part:
            current_part = ch.part
            if ch.part_title:
                print(f"  {ch.part_title}")
        chars = sum(len(p) for p in ch.paragraphs)
        print(f"    {ch.chapter_id:>6}  {ch.title[:44]:44s} "
              f"{len(ch.paragraphs):4d} ¶  {chars:6,d} ch")
    print(f"\nAddress a chapter as --chapter {chapters[0].chapter_id} "
          f"or by position --chapter 1")
    return 0


# ---------------------------------------------------------------------------
# fcc extract
# ---------------------------------------------------------------------------

def cmd_extract(args, cfg) -> int:
    epub = args.epub or cfg.get("epub")
    if not epub:
        return _fail("no EPUB given; pass --epub or set epub in flashcards.toml")

    title, chapters = ingest.read_chapters(Path(epub))
    chapter = ingest.select_chapter(chapters, args.chapter)
    book = args.book or cfg.get("book") or title

    print(f"{book} — {chapter.label}")
    print(f"  {len(chapter.paragraphs)} paragraphs, "
          f"{sum(len(p) for p in chapter.paragraphs):,} characters")

    # A chapter can be far too long to review in one sitting -- this book
    # ranges from 1.2k to 68k characters -- so --split-at breaks one into
    # several review-sized parts, each behaving as its own chapter downstream.
    batches = ingest.split_paragraphs(chapter, args.split_at)
    known_forms = lexicon.known_forms()
    total_parts = len(batches)

    for part, paragraphs in enumerate(batches, start=1):
        chapter_id = (chapter.chapter_id if total_parts == 1
                      else f"{chapter.chapter_id}p{part}")
        label = (chapter.label if total_parts == 1
                 else f"{chapter.label} (part {part} of {total_parts})")
        out_dir = paths.chapter_dir(_book_slug(book), chapter_id)
        artifacts.guard_overwrite(out_dir, "words", args.force)

        incipit = chapter.incipit() if total_parts == 1 else _incipit_of(paragraphs)
        artifacts.write(out_dir, "paragraphs", {
            "book": book,
            "chapter_id": chapter_id,
            "chapter_index": chapter.index,
            "chapter_number": chapter.number,
            "chapter_title": chapter.title,
            "part": chapter.part,
            "part_title": chapter.part_title,
            "label": label,
            "incipit": incipit,
            "paragraphs": [
                {"index": i, "text": text}
                for i, text in enumerate(paragraphs, start=1)
            ],
        })

        if total_parts > 1:
            print(f"  part {part}/{total_parts}: {len(paragraphs)} paragraphs")
        print("  tagging with spaCy…")
        candidates = nlp.extract_candidates(paragraphs, known_forms=known_forms)
        rows = [c.to_dict() for c in candidates]

        artifacts.write(out_dir, "words", {
            "book": book, "chapter_id": chapter_id, "label": label,
            "words": rows,
        })
        print(f"  {len(rows)} distinct (lemma, part of speech) candidates")
        print(f"  wrote {out_dir}/words.json")

    if total_parts > 1:
        print(f"\nnext: fcc level --chapter {chapter.chapter_id}p1  "
              f"(…through p{total_parts})")
    else:
        print(f"\nnext: fcc level --chapter {args.chapter}")
    return 0


def _incipit_of(paragraphs: list[str], limit: int = 200) -> str:
    """Opening line of a split part, so each part identifies itself."""
    if not paragraphs:
        return ""
    text = " ".join(paragraphs)[: limit * 2].strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


# ---------------------------------------------------------------------------
# fcc level
# ---------------------------------------------------------------------------

def cmd_level(args, cfg) -> int:
    book, chapter_id, out_dir = _resolve_dir(args, cfg)
    doc = artifacts.read(out_dir, "words")
    artifacts.guard_overwrite(out_dir, "words_leveled", args.force)

    threshold = args.min_level or cfg.get("min_level", "B2")
    known = store.load() if not args.ignore_known else {}

    rows = leveling.level_candidates(
        doc["words"], threshold=threshold, known=known,
        include_unknown=args.include_unknown)
    summary = leveling.summarise(rows)

    artifacts.write(out_dir, "words_leveled", {
        "book": book,
        "chapter_id": chapter_id,
        "label": doc.get("label", ""),
        "threshold": threshold,
        "include_unknown": args.include_unknown,
        "summary": summary,
        "words": rows,
    })

    print(f"{doc.get('label', chapter_id)}")
    print(f"  {summary['total']} words leveled")
    print("  by level:  " + "  ".join(
        f"{lv}:{n}" for lv, n in summary["by_level"].items()))
    print("  by source: " + "  ".join(
        f"{src}:{n}" for src, n in summary["by_source"].items()))
    print(f"  at {threshold}+: {summary['passing']}   "
          f"already known: {summary['already_known']}")
    print(f"\n  >>> {summary['candidates']} candidates for the deck")
    print(f"\nwrote {out_dir}/words_leveled.json")
    print("next: select the words you want (the french-flashcards skill will "
          "do this with your criteria), then `fcc enrich`")
    return 0


# ---------------------------------------------------------------------------
# fcc enrich
# ---------------------------------------------------------------------------

def _selected_rows(out_dir: Path, leveled: dict) -> tuple[list[dict], str]:
    """Rows chosen for the deck: a hand-made selection if present, else the
    mechanical default."""
    if artifacts.exists(out_dir, "selection"):
        doc = artifacts.read(out_dir, "selection")
        keep = {(d["lemma"], d["pos"]) for d in doc.get("decisions", [])
                if d.get("keep")}
        rows = [r for r in leveled["words"] if (r["lemma"], r["pos"]) in keep]
        return rows, doc.get("criteria") or "custom selection"

    rows = [r for r in leveled["words"]
            if r["passes_threshold"] and not r["already_known"]]
    return rows, f"default: {leveled.get('threshold', 'B2')}+, excluding known"


def cmd_enrich(args, cfg) -> int:
    book, chapter_id, out_dir = _resolve_dir(args, cfg)
    leveled = artifacts.read(out_dir, "words_leveled")
    artifacts.guard_overwrite(out_dir, "words_enriched", args.force)

    rows, criteria = _selected_rows(out_dir, leveled)
    if not rows:
        return _fail("selection is empty; nothing to enrich")

    print(f"{leveled.get('label', chapter_id)}")
    print(f"  {len(rows)} words ({criteria})")

    enriched: list[dict] = []
    for row in rows:
        entry = glosses.lookup(row["lemma"], row["pos"])
        merged = dict(row)
        merged.update(entry.to_dict())
        enriched.append(merged)

    cover = glosses.coverage(enriched)
    print(f"  dictionary: {cover['with_glosses']}/{cover['total']} glossed, "
          f"{cover['with_audio']} with audio in the index")

    # Sense-picking.
    if not args.no_senses:
        items = [
            llm.Item(lemma=r["lemma"], pos=r["pos"],
                     sentence=r.get("sentence", ""), candidates=r.get("glosses") or [])
            for r in enriched
        ]
        try:
            senses = llm.resolve_senses(
                items, backend=args.llm_backend,
                model=args.model or cfg.get("model", llm.DEFAULT_MODEL),
                pending_path=out_dir / "pending_senses.json",
                progress=lambda m: print(f"  {m}"))
        except llm.PendingSenses as pending:
            print(f"\n  {pending}")
            return 2
        for row, item in zip(enriched, items):
            picked = senses.get(item.key) or {}
            row["translation"] = picked.get("translation") or (
                row.get("glosses") or [""])[0]
            row["note"] = picked.get("note", "")
            row["translation_source"] = picked.get("source", "wiktionary")
    else:
        for row in enriched:
            row["translation"] = (row.get("glosses") or [""])[0]
            row["note"] = ""
            row["translation_source"] = "wiktionary"

    # Audio.
    if not args.no_audio:
        print("  fetching audio…")
        got = 0
        for row in enriched:
            result = audio.fetch_audio(
                row["lemma"], index_url=row.get("audio_url", ""),
                credit=row.get("audio_credit", ""),
                allow_live=not args.no_live_audio,
                allow_tts=args.tts_fallback)
            if result.ok:
                row["audio_path"] = str(result.path)
                row["credits"] = audio.credit_line(row["lemma"], result)
                got += 1
            else:
                row["audio_path"] = ""
                row["credits"] = ""
        print(f"  audio for {got}/{len(enriched)} words")

    artifacts.write(out_dir, "words_enriched", {
        "book": book,
        "chapter_id": chapter_id,
        "label": leveled.get("label", ""),
        "criteria": criteria,
        "threshold": leveled.get("threshold", "B2"),
        "words": enriched,
    })
    print(f"\nwrote {out_dir}/words_enriched.json")
    print(f"next: fcc build --chapter {args.chapter}")
    return 0


# ---------------------------------------------------------------------------
# fcc build
# ---------------------------------------------------------------------------

def cmd_build(args, cfg) -> int:
    book, chapter_id, out_dir = _resolve_dir(args, cfg)
    enriched = artifacts.read(out_dir, "words_enriched")
    paras = artifacts.read(out_dir, "paragraphs")

    meta = deck.DeckMeta(
        book=book,
        chapter_label=paras.get("label") or chapter_id,
        chapter_id=chapter_id,
        incipit=paras.get("incipit", ""),
        paragraphs=len(paras.get("paragraphs", [])),
    )
    output = Path(args.output) if args.output else (
        out_dir / f"{paths.slugify(book)}-ch{chapter_id}.apkg")

    result = deck.build_deck(
        enriched["words"], meta, output,
        threshold=enriched.get("threshold", "B2"),
        incipit_card=not args.no_incipit_card)

    print(f"built {result['path']}")
    print(f"  deck: {result['deck']}")
    print(f"  {result['cards']} cards across {result['paragraphs']} paragraphs, "
          f"{result['media_files']} audio files")
    if result["incipit_card"]:
        print(f"  incipit card: « {meta.incipit[:70]}… »")

    if not args.no_record:
        known = store.load()
        added = store.record(known, enriched["words"], book, chapter_id)
        store.save(known)
        print(f"  recorded {added} words as seen ({len(known)} total)")
    return 0


# ---------------------------------------------------------------------------
# fcc mark-known / stats
# ---------------------------------------------------------------------------

def cmd_run(args, cfg) -> int:
    """Build every chapter in one pass, then pack them into one .apkg.

    Runs in a single process on purpose: spaCy and ~45MB of lexicons load once
    instead of 59 times, and the seen-store threads through the loop so each
    chapter only surfaces words no earlier chapter already taught.
    """
    epub = args.epub or cfg.get("epub")
    if not epub:
        return _fail("no EPUB given; pass --epub or set epub in flashcards.toml")

    started = time.monotonic()
    title, chapters = ingest.read_chapters(Path(epub))
    book = args.book or cfg.get("book") or title
    slug = _book_slug(book)
    threshold = args.min_level or cfg.get("min_level", "B2")
    model = args.model or cfg.get("model", llm.DEFAULT_MODEL)

    if args.chapters:
        wanted = {_normalise_spec(c) for c in args.chapters.split(",")}
        chapters = [c for c in chapters if c.chapter_id in wanted]
        if not chapters:
            return _fail(f"no chapters matched {args.chapters!r}")

    print(f"{book} — {len(chapters)} chapters, threshold {threshold}+")
    print("loading spaCy and reference lexicons…")
    known_forms = lexicon.known_forms()
    known = {} if args.ignore_known else store.load()

    # ---- stages 1 and 2, chapter by chapter -------------------------------
    print("\nextracting and levelling…")
    staged: list[dict] = []
    for chapter in chapters:
        out_dir = paths.chapter_dir(slug, chapter.chapter_id)
        candidates = nlp.extract_candidates(
            chapter.paragraphs, known_forms=known_forms)
        rows = leveling.level_candidates(
            [c.to_dict() for c in candidates], threshold=threshold,
            known=known, include_unknown=args.include_unknown)

        artifacts.write(out_dir, "paragraphs", {
            "book": book, "chapter_id": chapter.chapter_id,
            "chapter_index": chapter.index, "chapter_number": chapter.number,
            "chapter_title": chapter.title, "part": chapter.part,
            "part_title": chapter.part_title, "label": chapter.label,
            "incipit": chapter.incipit(),
            "paragraphs": [{"index": i, "text": t}
                           for i, t in enumerate(chapter.paragraphs, start=1)],
        })
        artifacts.write(out_dir, "words_leveled", {
            "book": book, "chapter_id": chapter.chapter_id,
            "label": chapter.label, "threshold": threshold,
            "summary": leveling.summarise(rows), "words": rows,
        })

        fresh = [r for r in rows
                 if r["passes_threshold"] and not r["already_known"]]
        # Reserve these now so a later chapter does not re-teach them.
        for row in fresh:
            known[f"{row['lemma']}|{row['pos']}"] = {"lemma": row["lemma"]}
        staged.append({"chapter": chapter, "dir": out_dir, "rows": fresh})
        print(f"  {chapter.chapter_id}  {len(chapter.paragraphs):4d} ¶  "
              f"{len(rows):5d} words  {len(fresh):5d} new")

    pool = [r for s in staged for r in s["rows"]]
    print(f"\n{len(pool):,} distinct words at {threshold}+ across the book")

    # ---- stage 3: Claude judges each word ---------------------------------
    if not args.no_triage:
        criteria = args.criteria or "exclude arcane vocabulary"
        standing = config.standing_criteria(cfg)
        print(f"\ntriage — {criteria!r}")
        verdicts = triage.judge(
            [triage.from_row(r) for r in pool],
            backend=args.llm_backend, model=model, workers=args.workers,
            extra_criteria=standing or None,
            progress=lambda m: print(f"  {m}"))

        kept_keys = set()
        for stage in staged:
            selection = triage.build_selection(stage["rows"], verdicts, criteria)
            artifacts.write(stage["dir"], "selection", selection)
            stage["rows"] = [r for r in stage["rows"]
                             if verdicts.get(triage.from_row(r).key, {}).get("keep", True)]
            kept_keys.update(f"{r['lemma']}|{r['pos']}" for r in stage["rows"])

        dropped = len(pool) - len(kept_keys)
        print(f"  kept {len(kept_keys):,}  dropped {dropped:,} "
              f"({100 * dropped // max(len(pool), 1)}%)")

        # A failed batch keeps its words rather than losing them, which is the
        # right call -- but it means an unjudged word looks exactly like an
        # approved one in the totals. Say so loudly: a run that quietly admits
        # a quarter of the deck unreviewed should never look like a clean one.
        unjudged = sum(1 for v in verdicts.values() if v.get("source") == "error")
        if unjudged:
            print(f"  WARNING: {unjudged:,} words were kept without being judged "
                  f"({100 * unjudged // max(len(pool), 1)}% of the deck) because "
                  f"their batch failed.\n"
                  f"           Re-run the same command to judge just those -- "
                  f"failures are not cached.")

    pool = [r for s in staged for r in s["rows"]]
    print(f"{len(pool):,} words to card")

    # ---- stage 4: glosses, senses, audio ----------------------------------
    print("\nenriching…")
    for row in pool:
        entry = glosses.lookup(row["lemma"], row["pos"])
        row.update(entry.to_dict())

    if not args.no_senses:
        items = [llm.Item(lemma=r["lemma"], pos=r["pos"],
                          sentence=r.get("sentence", ""),
                          candidates=r.get("glosses") or []) for r in pool]
        senses = llm.resolve_senses(
            items, backend=args.llm_backend, model=model,
            workers=args.workers, progress=lambda m: print(f"  {m}"))
        for row, item in zip(pool, items):
            picked = senses.get(item.key) or {}
            row["translation"] = picked.get("translation") or (
                row.get("glosses") or [""])[0]
            row["note"] = picked.get("note", "")
    else:
        for row in pool:
            row["translation"] = (row.get("glosses") or [""])[0]
            row["note"] = ""

    if not args.no_audio:
        found = audio.fetch_many(
            pool, allow_live=not args.no_live_audio,
            allow_tts=not args.no_tts, workers=args.audio_workers,
            allow_download=args.download_audio,
            progress=lambda m: print(f"{m}"))
        native = sum(1 for r in pool if r.get("audio_path")
                     and not r["audio_path"].endswith(".tts.mp3"))
        print(f"  audio for {found:,}/{len(pool):,} words "
              f"({native:,} native, {found - native:,} synthesised)")

    # ---- stage 5: one package, plus per-chapter files ---------------------
    print("\nbuilding decks…")
    book_chapters = []
    for stage in staged:
        if not stage["rows"]:
            continue
        chapter = stage["chapter"]
        artifacts.write(stage["dir"], "words_enriched", {
            "book": book, "chapter_id": chapter.chapter_id,
            "label": chapter.label, "threshold": threshold,
            "criteria": args.criteria or "exclude arcane vocabulary",
            "words": stage["rows"],
        })
        meta = deck.DeckMeta(
            book=book, chapter_label=chapter.label,
            chapter_id=chapter.chapter_id, incipit=chapter.incipit(),
            paragraphs=len(chapter.paragraphs))
        book_chapters.append((stage["rows"], meta))
        if not args.no_per_chapter:
            deck.build_deck(
                stage["rows"], meta,
                stage["dir"] / f"{slug}-ch{chapter.chapter_id}.apkg",
                threshold=threshold, incipit_card=not args.no_incipit_card)

    output = Path(args.output) if args.output else (
        paths.WORK_DIR / slug / f"{slug}-complete.apkg")
    result = deck.build_book(
        book_chapters, output, threshold=threshold,
        incipit_card=not args.no_incipit_card)

    if not args.no_record:
        recorded = store.load()
        added = 0
        for stage in staged:
            added += store.record(recorded, stage["rows"], book,
                                  stage["chapter"].chapter_id)
        store.save(recorded)
        print(f"  recorded {added:,} words as seen")

    elapsed = time.monotonic() - started
    print(f"\nbuilt {result['path']}")
    print(f"  {result['decks']} chapter subdecks, {result['cards']:,} cards, "
          f"{result['media_files']:,} audio files")
    print(f"  {elapsed / 60:.1f} minutes")
    return 0


def cmd_mark_known(args, cfg) -> int:
    known = store.load()
    if args.forget:
        removed = store.forget(known, args.words)
        store.save(known)
        print(f"forgot {removed} words ({len(known)} remain)")
        return 0

    entries = [{"lemma": w, "pos": args.pos, "level": None} for w in args.words]
    added = store.record(known, entries, "manual", "-", source="manual")
    store.save(known)
    print(f"marked {added} words as known ({len(known)} total)")
    return 0


def cmd_stats(args, cfg) -> int:
    data = store.stats(store.load())
    print(f"{data['total']} words recorded as seen")
    if data["by_level"]:
        print("  by level: " + "  ".join(
            f"{k}:{v}" for k, v in sorted(data["by_level"].items())))
    for chapter, count in sorted(data["by_chapter"].items()):
        print(f"  {chapter}: {count}")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fcc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, chapter=True):
        p.add_argument("--book", help="book name (defaults to flashcards.toml)")
        if chapter:
            p.add_argument("--chapter", required=True,
                           help="chapter, as '1.6' (part.chapter) or '6' (position)")
        p.add_argument("--force", action="store_true",
                       help="overwrite this stage's existing output")

    p = sub.add_parser("chapters", help="list the chapters in an EPUB")
    p.add_argument("--epub")
    p.add_argument("--book")
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser("extract", help="EPUB -> candidate words")
    p.add_argument("--epub")
    p.add_argument("--split-at", type=int, metavar="CHARS",
                   help="split an oversized chapter into review-sized parts, "
                        "addressed as 1.1p1, 1.1p2, …")
    common(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("level", help="assign CEFR levels")
    common(p)
    p.add_argument("--min-level", help="threshold, default B2")
    p.add_argument("--include-unknown", action="store_true",
                   help="keep words neither lexicon recognises (mostly noise)")
    p.add_argument("--ignore-known", action="store_true",
                   help="ignore the seen-words store for this run")
    p.set_defaults(func=cmd_level)

    p = sub.add_parser("enrich", help="glosses, sense-picking and audio")
    common(p)
    p.add_argument("--llm-backend", default="auto", choices=("auto",) + llm.BACKENDS)
    p.add_argument("--model", help=f"default {llm.DEFAULT_MODEL}")
    p.add_argument("--no-senses", action="store_true",
                   help="skip sense-picking, use the first dictionary sense")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--no-live-audio", action="store_true",
                   help="use only audio already in the local index")
    p.add_argument("--tts-fallback", action="store_true",
                   help="synthesise speech for words with no recording")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("build", help="write the .apkg")
    common(p)
    p.add_argument("--output")
    p.add_argument("--no-incipit-card", action="store_true")
    p.add_argument("--no-record", action="store_true",
                   help="do not add these words to the seen-words store")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("run", help="build every chapter and pack them into one .apkg")
    p.add_argument("--epub")
    p.add_argument("--book")
    p.add_argument("--chapters", help="comma-separated subset, e.g. '1.1,1.2'")
    p.add_argument("--min-level", help="threshold, default B2")
    p.add_argument("--criteria", help="what to keep or drop, e.g. "
                                      "'exclude arcane vocabulary'")
    p.add_argument("--no-triage", action="store_true",
                   help="skip Claude's keep/drop judgement")
    p.add_argument("--llm-backend", default="auto", choices=("auto",) + llm.BACKENDS)
    p.add_argument("--model")
    p.add_argument("--workers", type=int, default=llm.DEFAULT_WORKERS,
                   help="concurrent LLM batches")
    p.add_argument("--audio-workers", type=int, default=8)
    p.add_argument("--include-unknown", action="store_true")
    p.add_argument("--ignore-known", action="store_true")
    p.add_argument("--no-senses", action="store_true")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--no-live-audio", action="store_true")
    p.add_argument("--no-tts", action="store_true",
                   help="do not synthesise audio for words with no recording")
    p.add_argument("--download-audio", action="store_true",
                   help="try downloading native recordings. Off by default for "
                        "whole-book runs: Wikimedia throttles bulk fetching hard "
                        "(~83%% 429s here, regardless of pacing). Cached native "
                        "audio is always used and always preferred.")
    p.add_argument("--no-incipit-card", action="store_true")
    p.add_argument("--no-per-chapter", action="store_true",
                   help="only write the combined package")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--output")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("mark-known", help="record words as already known")
    p.add_argument("words", nargs="+")
    p.add_argument("--pos", default="NOUN")
    p.add_argument("--forget", action="store_true", help="remove them instead")
    p.set_defaults(func=cmd_mark_known)

    p = sub.add_parser("stats", help="what the seen-words store contains")
    p.set_defaults(func=cmd_stats)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config.load()
    try:
        return args.func(args, cfg)
    except (artifacts.ArtifactError, ingest.IngestError, lexicon.LexiconError,
            nlp.NlpError, deck.DeckError, llm.LlmError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
