---
name: french-flashcards
description: Build Anki flashcard decks of French vocabulary from a book chapter, graded by CEFR level. Use when the user wants to extract vocabulary from French reading material, level words by CEFR, curate a word list before reading, or generate an Anki deck for a chapter.
---

# French reading → Anki deck

Turns one chapter of a French book into an Anki deck of the vocabulary that is
actually new to the reader at B2, with cards grouped by paragraph so they can
be reviewed in reading order before starting the chapter.

Run from the repository root. `fcc` is the CLI (`.venv/bin/fcc` if the venv is
not active).

## The five stages

| # | Stage | Command | Who does it |
|---|---|---|---|
| 1 | Extract | `fcc extract --chapter 1.6` | code |
| 2 | Level | `fcc level --chapter 1.6` | code |
| 3 | **Select** | *you, with the user's criteria* | **judgement** |
| 4 | Enrich | `fcc enrich --chapter 1.6` | code + you |
| 5 | Build | `fcc build --chapter 1.6` | code |

Each stage writes a JSON file into `work/<book>/ch<id>/`, and reads the
previous one. Any stage can be re-run alone. `--force` overwrites.

**Always stop after stage 2 and ask what to keep.** That pause is the whole
point of the staging. Only skip it if the user has said to run straight
through.

## Chapters

`fcc chapters` lists them. Address a chapter either as `1.6` (Livre I,
chapter VI) or by flat position `6` — both work at every stage.

## Stage 3: selection

This is the stage that needs you. Read `words_leveled.json`, decide what
belongs in the deck, and write `selection.json` next to it:

```json
{
  "schema_version": 1,
  "stage": "selection",
  "criteria": "exclude words which seem silly",
  "decisions": [
    {"lemma": "basoche", "pos": "NOUN", "keep": false,
     "reason": "medieval legal guild, 0.07/M in modern French — no use outside Hugo"},
    {"lemma": "ruisseler", "pos": "VERB", "keep": true,
     "reason": "vivid everyday verb, 19/M in modern French"}
  ]
}
```

Rules:

- **Every dropped word needs a written reason.** The user must be able to see
  why, and overrule it.
- **Include a decision for every word you kept too**, not just the drops.
- Default with no criteria: keep `passes_threshold && !already_known`.
- Then report a summary — counts by level, how many kept and dropped, and a
  sample of the drops — before moving on.
- If the kept count looks wrong (a handful, or many hundreds), say so and
  check before building.

### Judging "silly" or "not worth learning"

Use `modern_freq`, which is occurrences per million in modern French books.
CEFR level alone cannot make this call — these are all C1/C2:

| `modern_freq` | Example | Verdict |
|---|---|---|
| ~0.07 | `basoche`, `surcot`, `haut-de-chausses` | period-specific, usually drop |
| 15–45 | `tournoyer`, `ruisseler`, `perron` | genuinely useful, keep |

A word can still be worth keeping at low frequency if it recurs in the chapter
(`count`), since the reader will meet it repeatedly. Weigh both.

`flashcards.toml` holds standing criteria under `[selection]` that apply to
every chapter. Apply those *and* whatever the user asks for this time.

## Stage 4: enrichment

`fcc enrich --chapter 1.6` adds glosses, gender, IPA, audio, and picks the
dictionary sense that fits each sentence.

Sense-picking needs no API key. By default it shells out to the Claude Code
CLI. To do it yourself in the conversation instead, use
`--llm-backend conversation`: enrich writes `pending_senses.json`, exits with
status 2, and you write the answers to `resolved_senses.json` in the same
directory as `{"answers": [{"key": ..., "translation": ..., "note": ...}]}`,
then re-run the same command.

Keep translations short — a word or brief phrase, the way a dictionary
headword reads. Notes only where genuinely useful (register, false friend,
typical collocation), otherwise empty.

After enriching, show the user the translations before building so they can
correct any.

## Stage 5: build

`fcc build --chapter 1.6` writes the `.apkg` and records the words in the
seen-store so later chapters skip them. Report the path and card count.

`--no-record` builds without touching the store, for a trial run.

## Other commands

- `fcc stats` — what the seen-words store holds
- `fcc mark-known <words...>` — retire words the user already knows
- `fcc mark-known <words...> --forget` — undo that

## Notes

- The reference data must exist: `python scripts/fetch_data.py` (once, ~573MB
  download). If a stage reports a missing index, that is the fix.
- Deck size varies enormously by chapter. Notre-Dame's Livre I ch. I yields
  ~420 candidates; ch. VI yields ~56. For the big ones, either curate hard at
  stage 3 or use `fcc extract --split-at 20000`.
- Words neither reference lexicon recognises are excluded by default; they are
  mostly tagger noise. `fcc level --include-unknown` keeps them.
