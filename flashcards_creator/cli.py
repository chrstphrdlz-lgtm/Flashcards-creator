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
from pathlib import Path
from typing import Any

from . import (artifacts, audio, config, deck, glosses, ingest, leveling,
               lexicon, llm, nlp, paths, store)


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
    out_dir = paths.chapter_dir(_book_slug(book), chapter.chapter_id)

    artifacts.guard_overwrite(out_dir, "words", args.force)

    print(f"{book} — {chapter.label}")
    print(f"  {len(chapter.paragraphs)} paragraphs, "
          f"{sum(len(p) for p in chapter.paragraphs):,} characters")

    artifacts.write(out_dir, "paragraphs", {
        "book": book,
        "chapter_id": chapter.chapter_id,
        "chapter_index": chapter.index,
        "chapter_number": chapter.number,
        "chapter_title": chapter.title,
        "part": chapter.part,
        "part_title": chapter.part_title,
        "label": chapter.label,
        "incipit": chapter.incipit(),
        "paragraphs": [
            {"index": i, "text": text}
            for i, text in enumerate(chapter.paragraphs, start=1)
        ],
    })

    print("  tagging with spaCy…")
    candidates = nlp.extract_candidates(
        chapter.paragraphs, known_forms=lexicon.known_forms())
    rows = [c.to_dict() for c in candidates]

    artifacts.write(out_dir, "words", {
        "book": book,
        "chapter_id": chapter.chapter_id,
        "label": chapter.label,
        "words": rows,
    })
    print(f"  {len(rows)} distinct (lemma, part of speech) candidates")
    print(f"\nwrote {out_dir}/words.json\nnext: fcc level --chapter {args.chapter}")
    return 0


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
