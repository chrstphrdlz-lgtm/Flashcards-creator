"""Stage 3, automated: Claude judges whether each word is worth learning.

Built for the case where a chapter-by-chapter conversation is impractical --
the whole of Notre-Dame de Paris yields 5,364 distinct B2+ words.

**Why a model and not a threshold.** Measured across the book, arcane
vocabulary does not separate on frequency. Excluding everything under one
occurrence per million removes only 813 of those 5,364 words, and the words it
removes are not the right ones: ``capitulaire`` and ``courtisan`` sit in the
same frequency band, as do ``houppe`` and ``magistrature``. Frequency ranks
words; it cannot decide them. So it is passed to the model as evidence rather
than used as a filter.

Output is the same ``selection.json`` a hand-made selection produces, with a
written reason against every decision, so nothing is dropped invisibly and any
call can be overruled by editing the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from . import llm

BATCH_SIZE = 40

SYSTEM_PROMPT = (
    "You are helping an upper-intermediate (B2) learner of French decide which "
    "vocabulary from a 19th-century novel is worth making flashcards for. The "
    "learner wants to read French literature comfortably AND use French in "
    "ordinary life. You are ruthless about wasting their review time. Output "
    "only valid JSON, no markdown fences and no prose."
)

INSTRUCTION = """\
For each word decide whether it earns a flashcard.

DROP it when the word is:
  - obsolete or archaic in modern French
  - specific to medieval life (law, church, armour, costume, architecture,
    trades) with no currency today
  - an antique spelling or form of a word that survives in another shape
  - a proper noun, place name, or foreign word the tagger mistook for French
  - so technical that this book is the only place the learner would meet it

KEEP it when the word is:
  - current in modern French, even if formal or literary
  - still alive figuratively, though its literal sense is dated
  - common across literature generally, not just in this book

When you are genuinely torn, DROP. A card kept "just in case" costs the learner
real time. The exception is recurrence: a word appearing many times in this book
will be met again and again while reading, so it earns its place even when rare
in modern usage.

You are given, per word: lemma, part of speech, CEFR level, `modern_freq`
(occurrences per million in modern French books -- low means rare TODAY, which
is evidence of arcaneness but not proof), `count` (times it occurs in this
book), and a sentence from the text.

Return a JSON array, one object per word:
{"id": <int>, "keep": <true|false>, "reason": "<under 12 words>"}
Give a reason for every word, kept or dropped. Return every id.\
"""


@dataclass
class Candidate:
    lemma: str
    pos: str
    level: str
    modern_freq: float | None
    count: int
    sentence: str

    @property
    def key(self) -> str:
        # Namespaced so triage verdicts never collide with sense-picking
        # entries in the shared cache.
        return f"triage:{self.lemma}|{self.pos}"

    def payload(self, index: int) -> dict[str, Any]:
        return {
            "id": index,
            "lemma": self.lemma,
            "pos": self.pos,
            "level": self.level,
            "modern_freq": self.modern_freq,
            "count": self.count,
            "sentence": self.sentence[:300],
        }


def from_row(row: dict) -> Candidate:
    return Candidate(
        lemma=row["lemma"],
        pos=row.get("pos", ""),
        level=row.get("level", ""),
        modern_freq=row.get("modern_freq"),
        count=int(row.get("count") or 1),
        sentence=row.get("sentence", ""),
    )


def _build_prompt(batch: list[Candidate]) -> str:
    items = [c.payload(i) for i, c in enumerate(batch)]
    return f"{INSTRUCTION}\n\n{json.dumps(items, ensure_ascii=False)}"


def judge(
    candidates: list[Candidate],
    backend: str = "auto",
    model: str = llm.DEFAULT_MODEL,
    workers: int = llm.DEFAULT_WORKERS,
    extra_criteria: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict]:
    """Return {candidate.key: {"keep": bool, "reason": str, "source": str}}.

    A batch that fails keeps its words rather than discarding them: silently
    losing vocabulary to a transport error would be far worse than a few
    unjudged cards, and the reason field says so plainly.
    """
    say = progress or (lambda _msg: None)
    cache = llm.load_cache()
    out: dict[str, dict] = {}

    todo = []
    for candidate in candidates:
        hit = cache.get(candidate.key)
        if hit and "keep" in hit:
            out[candidate.key] = hit
        else:
            todo.append(candidate)

    if not todo:
        say(f"all {len(candidates)} words already judged (cached)")
        return out

    chosen = llm.available_backend(backend)
    if chosen in ("none", "conversation"):
        # Triage is a bulk operation; without a usable backend, keep everything
        # and let the totals make it obvious that no judging happened.
        say(f"no triage backend available ({chosen}); keeping all words")
        for candidate in todo:
            out[candidate.key] = {
                "keep": True, "reason": "not judged (no LLM backend)",
                "source": "default"}
        return out

    say(f"judging {len(todo)} words with {chosen} "
        f"({(len(todo) + BATCH_SIZE - 1) // BATCH_SIZE} batches, {workers} at a time)")

    prompt_fn = _build_prompt
    if extra_criteria:
        joined = "\n".join(f"  - {c}" for c in extra_criteria)

        def prompt_fn(batch, _joined=joined):  # noqa: F811
            return (f"{INSTRUCTION}\n\nAlso apply these instructions from the "
                    f"learner:\n{_joined}\n\n"
                    f"{json.dumps([c.payload(i) for i, c in enumerate(batch)], ensure_ascii=False)}")

    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    for batch, results in llm.run_batches(
            batches, prompt_fn, chosen, model, workers=workers, progress=say):
        if results is None:
            for candidate in batch:
                out[candidate.key] = {
                    "keep": True, "reason": "kept: judging failed for this batch",
                    "source": "error"}
            continue

        by_id = {r["id"]: r for r in results if isinstance(r.get("id"), int)}
        for index, candidate in enumerate(batch):
            result = by_id.get(index)
            if not result or "keep" not in result:
                out[candidate.key] = {
                    "keep": True, "reason": "kept: no verdict returned",
                    "source": "error"}
                continue
            entry = {
                "keep": bool(result["keep"]),
                "reason": str(result.get("reason") or "").strip(),
                "source": chosen,
            }
            out[candidate.key] = entry
            cache[candidate.key] = entry

    llm.save_cache(cache)
    return out


def build_selection(
    rows: list[dict],
    verdicts: dict[str, dict],
    criteria: str,
) -> dict[str, Any]:
    """Assemble the selection.json body from the verdicts."""
    decisions = []
    for row in rows:
        candidate = from_row(row)
        verdict = verdicts.get(candidate.key) or {}
        decisions.append({
            "lemma": row["lemma"],
            "pos": row["pos"],
            "level": row.get("level"),
            "modern_freq": row.get("modern_freq"),
            "count": row.get("count"),
            "keep": bool(verdict.get("keep", True)),
            "reason": verdict.get("reason") or "kept by default",
            "judged_by": verdict.get("source", "default"),
        })
    return {"criteria": criteria, "decisions": decisions}


def summarise(decisions: list[dict]) -> dict[str, Any]:
    kept = [d for d in decisions if d["keep"]]
    dropped = [d for d in decisions if not d["keep"]]
    by_level: dict[str, int] = {}
    for d in kept:
        by_level[d.get("level") or "?"] = by_level.get(d.get("level") or "?", 0) + 1
    return {
        "total": len(decisions),
        "kept": len(kept),
        "dropped": len(dropped),
        "kept_by_level": by_level,
        # Rarest first: the clearest look at whether the cut went too deep.
        "sample_dropped": sorted(
            dropped, key=lambda d: (d.get("modern_freq") if d.get("modern_freq")
                                    is not None else 0))[:25],
    }
