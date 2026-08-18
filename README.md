# Flashcards Creator

Turns a chapter of a French book into an Anki deck of the vocabulary that is
genuinely new to you, graded by CEFR level, with cards grouped by paragraph so
you can review a chapter's words in reading order before you start it.

Built for reading at B2. Cards carry an English translation chosen to fit the
sentence the word appeared in, native-speaker audio, gender, and IPA.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl
.venv/bin/python scripts/fetch_data.py     # once; downloads ~600MB, keeps ~45MB
```

Put your EPUB in `books/` and point `flashcards.toml` at it.

**No API key is required.** Sense-picking uses the Claude Code CLI's existing
login, or is done by Claude directly in a conversation.

## Use

```bash
fcc chapters                     # list the chapters
fcc extract --chapter 1.6        # EPUB      -> words.json
fcc level   --chapter 1.6        # words     -> words_leveled.json
#   ... choose what to keep ...  # -> selection.json
fcc enrich  --chapter 1.6        # selection -> words_enriched.json
fcc build   --chapter 1.6        # enriched  -> .apkg
```

Chapters are addressed as `1.6` (Livre I, chapter VI) or by flat position `6`.

Each stage reads a file and writes a file into `work/<book>/ch<id>/`. That is
the point: you can stop after any stage, read the JSON, edit it, and carry on.

### The selection stage

Stage 3 has no command, because it is the one that needs judgement. Ask Claude:

> Extract chapter 1.6 and build me a deck, but exclude words which seem silly

The [`french-flashcards` skill](.claude/skills/french-flashcards/SKILL.md)
drives the pipeline, stops after levelling, applies your criteria, and writes
`selection.json` with a **reason recorded against every word it drops** — so
nothing disappears silently and you can overrule it.

Standing preferences that apply to every chapter go in `flashcards.toml` under
`[selection]`.

## How a word's level is decided

A four-tier cascade. Every card records which tier judged it, in a
`LevelSource` field, so you can always see how a level was arrived at.

| Tier | Method | Source |
|---|---|---|
| 1 | exact match on (lemma, part of speech) | FLELex-Beacco |
| 2 | lemma match, dominant reading | FLELex-Beacco |
| 3 | not in FLELex → estimate from modern book frequency | Lexique 3.83 |
| 4 | in neither → excluded by default | — |

FLELex-Beacco is a CEFR-graded lexicon built by UCLouvain from French-as-a-
foreign-language textbooks and graded readers. It covers 14,236 lemmas, which a
novel exceeds, hence tier 3.

**Tier 3 is deliberately coarse, and says so.** Calibrated against FLELex, the
per-level frequency distributions overlap almost entirely: A2 and B1 have
near-identical medians (21.35 vs 21.82 per million) and C2 is *more* frequent
than C1. Frequency cannot separate those pairs, so the estimator reports an
agreement score per band rather than implying six-level precision.

## Judging which words are worth learning

CEFR level alone cannot tell you this. Every word below is C1 or C2:

| `modern_freq` | Examples | Worth learning? |
|---|---|---|
| ~0.07 / million | `basoche`, `surcot`, `haut-de-chausses` | rarely — period-specific |
| 15–45 / million | `tournoyer`, `ruisseler`, `perron` | yes |

So each word carries `modern_freq` alongside its level, giving an instruction
like *"exclude words which seem silly"* something concrete to act on.

## Keeping decks a sane size

A persistent seen-words store (`data/known_words.json`, keyed by lemma **and**
part of speech) means each chapter only surfaces words you have not been carded
on. The first chapter of a book is expensive; later ones shrink sharply.

Measured on *Notre-Dame de Paris*:

| Chapter | Paragraphs | Words | At B2+ |
|---|---|---|---|
| Livre I ch. I | 139 | 1,017 | ~423 |
| Livre I ch. VI | 25 | 170 | 56 |

Hugo is dense, and the long chapters are genuinely large. Trim them at the
selection stage, or use `fcc level --min-level C1` and
`fcc extract --split-at 20000`.

`fcc mark-known <words...>` retires words you already know; `fcc stats` shows
what the store holds.

## Telling decks apart

Each deck carries its chapter's opening line in two places — the deck
description, which Anki shows on the overview screen, and a first card, which
is unmissable:

```
LIVRE PREMIER, ch. VI — LA ESMERALDA
Commence : « Nous sommes ravis d'avoir à apprendre à nos lecteurs… »
56 cards · B2+ · Notre-Dame de Paris · built 2026-08-18
```

Disable the card with `--no-incipit-card`.

## Audio

Native-speaker recordings from Wikimedia Commons, mostly
[Lingua Libre](https://lingualibre.org), preferring French-from-France voices.
URLs are already in the local index, so finding audio costs no network; only
downloading does, rate-limited and cached across chapters.

Coverage is good even for obscure literary vocabulary — 54 of 56 words in the
test chapter. `--tts-fallback` synthesises the rest, labelled as synthetic on
the card.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The chapter-detection tests run against the real *Notre-Dame de Paris* EPUB and
skip if it is absent.

## Licences

The reference data is other people's work under CC BY-SA and CC BY-NC-SA
(non-commercial). See [CREDITS.md](CREDITS.md) — it matters if you share decks.
