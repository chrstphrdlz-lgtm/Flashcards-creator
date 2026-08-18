"""Sense-picking: choosing the translation that fits the sentence.

Wiktionary lists every sense a word has ever had. ``livre`` is both "book" and
"pound"; ``vaisseau`` is both "ship" and "blood vessel". Only the sentence the
word actually appeared in can decide which belongs on the card.

Four backends, none of which requires an API key:

``conversation``  the default under the skill. Writes the open questions to a
                  file, Claude answers them in the chat, the build resumes.
                  No subprocess, no extra cost, and you can see every choice.
``claude-cli``    shells out to the Claude Code CLI, which authenticates with
                  the OAuth session you already have. For unattended runs.
``api``           uses ANTHROPIC_API_KEY. Optional, for CI.
``none``          takes Wiktionary's first sense. Always available.

Everything is cached by (lemma, POS, sentence), so a re-run costs nothing and
later chapters reuse earlier decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import paths

BACKENDS = ("conversation", "claude-cli", "api", "none")

DEFAULT_MODEL = "claude-sonnet-5"

# Batched rather than one call per word: measured at 50 words per request,
# which returned valid JSON for all of them in about 11 seconds.
BATCH_SIZE = 50

SYSTEM_PROMPT = (
    "You are a French-English lexicographer preparing Anki flashcards for an "
    "upper-intermediate (B2) learner reading French literature. For each item "
    "you are given a French lemma, its part of speech, the sentence it appears "
    "in, and candidate dictionary senses. Choose the ONE English translation "
    "that fits the sentence. Keep it short -- a word or a brief phrase, the way "
    "a dictionary headword gloss reads. If the candidate senses are all wrong "
    "or absent, supply your own translation from the sentence. Output only "
    "valid JSON, no markdown fences and no prose."
)

INSTRUCTION = (
    'Return a JSON array. One object per item: {"id": <int>, "translation": '
    '<string>, "note": <string>}. "note" is a short usage hint where genuinely '
    'useful (register, false friend, typical collocation), otherwise "". '
    "Return every id you were given."
)


class LlmError(Exception):
    """Raised when a backend is unusable or returns nothing parseable."""


class PendingSenses(Exception):
    """Raised by the `conversation` backend to hand control back to Claude.

    Not an error: it means the build is waiting for sense decisions that the
    conversation is expected to supply.
    """

    def __init__(self, path: Path, count: int):
        super().__init__(
            f"{count} words need sense decisions. They have been written to "
            f"{path}. Resolve them, then re-run the same command."
        )
        self.path = path
        self.count = count


@dataclass
class Item:
    lemma: str
    pos: str
    sentence: str
    candidates: list[str]

    @property
    def key(self) -> str:
        digest = hashlib.sha1(self.sentence.encode("utf-8")).hexdigest()[:12]
        return f"{self.lemma}|{self.pos}|{digest}"

    def payload(self, index: int) -> dict[str, Any]:
        return {
            "id": index,
            "lemma": self.lemma,
            "pos": self.pos,
            "sentence": self.sentence,
            "candidates": self.candidates[:4],
        }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache(path: Path | None = None) -> dict[str, dict]:
    path = path or paths.LLM_CACHE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_cache(cache: dict[str, dict], path: Path | None = None) -> None:
    path = path or paths.LLM_CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def strip_fences(text: str) -> str:
    """Remove markdown code fences.

    Necessary in practice: the CLI wraps JSON in ```json fences even when the
    prompt explicitly forbids them.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_response(text: str) -> list[dict]:
    """Parse a batch response, tolerating surrounding prose."""
    cleaned = strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.S)
        if not match:
            raise LlmError(f"no JSON array in response: {cleaned[:200]!r}")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LlmError(f"malformed JSON in response: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("items") or data.get("results") or [data]
    if not isinstance(data, list):
        raise LlmError("expected a JSON array of results")
    return [d for d in data if isinstance(d, dict) and "id" in d]


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _build_prompt(batch: list[Item]) -> str:
    items = [item.payload(i) for i, item in enumerate(batch)]
    return f"{INSTRUCTION}\n\n{json.dumps(items, ensure_ascii=False)}"


def _run_claude_cli(prompt: str, model: str, timeout: int = 300) -> str:
    """Call the Claude Code CLI in headless mode.

    The prompt goes in on **stdin**, not as an argument: a realistic batch is
    several kilobytes and fails as an argv entry.

    The flags strip the CLI down to a plain model call -- without them it loads
    its full agent system prompt (~40k tokens versus ~19k), and would inherit
    this project's own settings and MCP servers, which have nothing to do with
    translating French.
    """
    binary = shutil.which("claude")
    if not binary:
        raise LlmError("the `claude` CLI is not on PATH")

    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
        "--exclude-dynamic-system-prompt-sections",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disallowed-tools",
        "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite",
        "--model", model,
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LlmError(f"claude CLI timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise LlmError(f"claude CLI failed ({proc.returncode}): "
                       f"{(proc.stderr or proc.stdout)[:300]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LlmError(f"claude CLI returned non-JSON: {proc.stdout[:200]!r}") from exc

    if envelope.get("is_error"):
        raise LlmError(f"claude CLI reported an error: {envelope.get('result')}")
    return envelope.get("result") or ""


def _run_api(prompt: str, model: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise LlmError("the `anthropic` package is not installed "
                       "(pip install 'flashcards-creator[api]')") from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LlmError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model, max_tokens=8000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content
                   if getattr(block, "type", "") == "text")


def available_backend(preferred: str | None = None) -> str:
    """Pick a backend, preferring whatever needs no configuration."""
    if preferred and preferred != "auto":
        if preferred not in BACKENDS:
            raise LlmError(f"unknown backend {preferred!r}; choose from {BACKENDS}")
        return preferred
    if shutil.which("claude"):
        return "claude-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return "none"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_senses(
    items: list[Item],
    backend: str = "auto",
    model: str = DEFAULT_MODEL,
    pending_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict]:
    """Return {item.key: {"translation", "note", "source"}} for every item.

    Never raises for want of an LLM: an unavailable backend degrades to the
    first Wiktionary sense, because a deck with imperfect glosses beats no deck.
    """
    say = progress or (lambda _msg: None)
    cache = load_cache()
    out: dict[str, dict] = {}

    todo = []
    for item in items:
        hit = cache.get(item.key)
        if hit:
            out[item.key] = hit
        else:
            todo.append(item)

    if not todo:
        say(f"all {len(items)} senses already cached")
        return out

    chosen = available_backend(backend)
    say(f"{len(todo)} words need sense-picking (backend: {chosen})")

    if chosen == "none":
        for item in todo:
            out[item.key] = _fallback(item)
        return out

    if chosen == "conversation":
        path = pending_path or (paths.DATA_DIR / "pending_senses.json")
        resolved = _read_resolved(path, todo)
        if resolved is not None:
            out.update(resolved)
            cache.update(resolved)
            save_cache(cache)
            return out
        _write_pending(path, todo)
        raise PendingSenses(path, len(todo))

    runner = _run_claude_cli if chosen == "claude-cli" else _run_api

    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        say(f"  batch {start // BATCH_SIZE + 1}: {len(batch)} words")
        try:
            raw = (runner(_build_prompt(batch), model) if chosen == "claude-cli"
                   else runner(_build_prompt(batch), model))
            results = parse_response(raw)
        except LlmError as exc:
            say(f"  batch failed ({exc}); falling back to dictionary order")
            for item in batch:
                out[item.key] = _fallback(item)
            continue

        by_id = {r["id"]: r for r in results if isinstance(r.get("id"), int)}
        for index, item in enumerate(batch):
            result = by_id.get(index)
            if not result or not (result.get("translation") or "").strip():
                out[item.key] = _fallback(item)
                continue
            entry = {
                "translation": str(result["translation"]).strip(),
                "note": str(result.get("note") or "").strip(),
                "source": chosen,
            }
            out[item.key] = entry
            cache[item.key] = entry

    save_cache(cache)
    return out


def _fallback(item: Item) -> dict:
    return {
        "translation": item.candidates[0] if item.candidates else "",
        "note": "",
        "source": "wiktionary",
    }


def _write_pending(path: Path, items: list[Item]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "instructions": (
            "For each item choose the ONE English translation that fits the "
            "sentence, keeping it to a word or short phrase. Add a short note "
            "only where genuinely useful, otherwise \"\". Then write the "
            "answers to the file named by `write_answers_to` as "
            "{\"answers\": [{\"key\": ..., \"translation\": ..., \"note\": ...}]} "
            "and re-run the same command."
        ),
        "write_answers_to": str(path.with_name("resolved_senses.json")),
        "items": [
            {
                "key": item.key,
                "lemma": item.lemma,
                "pos": item.pos,
                "sentence": item.sentence,
                "candidates": item.candidates[:4],
            }
            for item in items
        ],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _read_resolved(path: Path, items: list[Item]) -> dict[str, dict] | None:
    """Read answers written by the conversation, if they are all there yet."""
    answers_path = path.with_name("resolved_senses.json")
    if not answers_path.exists():
        return None
    try:
        doc = json.loads(answers_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    answers = doc.get("answers") if isinstance(doc, dict) else doc
    if not isinstance(answers, list):
        return None

    by_key = {
        a["key"]: a for a in answers
        if isinstance(a, dict) and a.get("key") and a.get("translation")
    }
    wanted = {item.key for item in items}
    if not wanted <= set(by_key):
        return None  # incomplete; ask again rather than silently part-filling

    resolved = {}
    for item in items:
        answer = by_key[item.key]
        resolved[item.key] = {
            "translation": str(answer["translation"]).strip(),
            "note": str(answer.get("note") or "").strip(),
            "source": "conversation",
        }
    answers_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return resolved
