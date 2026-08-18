"""Stage artifacts: the files that flow between pipeline stages.

Each stage reads one artifact and writes the next. Keeping them as plain JSON on
disk is what makes the pipeline interruptible -- you can stop after any stage,
read the file, hand-edit it, and resume.

Every artifact carries a ``stage`` and ``schema_version`` header so a stage can
refuse mismatched input loudly instead of failing in a confusing way ten steps
later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Ordered, so we can tell a user they are trying to run stages out of sequence.
STAGES = ["paragraphs", "words", "words_leveled", "selection", "words_enriched"]

FILENAMES = {
    "paragraphs": "paragraphs.json",
    "words": "words.json",
    "words_leveled": "words_leveled.json",
    "selection": "selection.json",
    "words_enriched": "words_enriched.json",
}


class ArtifactError(Exception):
    """Raised when an artifact is missing, malformed, or from another version."""


@dataclass
class Artifact:
    stage: str
    payload: dict[str, Any]

    @property
    def body(self) -> dict[str, Any]:
        return self.payload


def path_for(chapter_dir: Path, stage: str) -> Path:
    if stage not in FILENAMES:
        raise ArtifactError(f"unknown stage {stage!r}; expected one of {STAGES}")
    return chapter_dir / FILENAMES[stage]


def write(chapter_dir: Path, stage: str, payload: dict[str, Any]) -> Path:
    """Write an artifact, stamping it with its stage and schema version."""
    chapter_dir.mkdir(parents=True, exist_ok=True)
    dest = path_for(chapter_dir, stage)
    doc = {"schema_version": SCHEMA_VERSION, "stage": stage, **payload}
    dest.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return dest


def read(chapter_dir: Path, stage: str) -> dict[str, Any]:
    """Read an artifact, validating its header.

    The error messages name the command that produces the missing file, since
    the most common mistake is running a stage before the one that feeds it.
    """
    src = path_for(chapter_dir, stage)
    if not src.exists():
        raise ArtifactError(
            f"missing {src.name} in {chapter_dir}\n"
            f"  run the stage that produces it first ({_producer(stage)})"
        )
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{src} is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise ArtifactError(f"{src} should contain a JSON object")

    found = doc.get("stage")
    if found != stage:
        raise ArtifactError(f"{src} is a {found!r} artifact, expected {stage!r}")

    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ArtifactError(
            f"{src} has schema_version {version}, this build expects "
            f"{SCHEMA_VERSION}. Re-run the pipeline from `fcc extract`."
        )
    return doc


def exists(chapter_dir: Path, stage: str) -> bool:
    return path_for(chapter_dir, stage).exists()


def _producer(stage: str) -> str:
    return {
        "paragraphs": "fcc extract",
        "words": "fcc extract",
        "words_leveled": "fcc level",
        "selection": "the select stage -- see the french-flashcards skill",
        "words_enriched": "fcc enrich",
    }.get(stage, "an earlier stage")


def guard_overwrite(chapter_dir: Path, stage: str, force: bool) -> None:
    """Refuse to clobber a hand-edited downstream artifact without --force.

    Stage 3 output in particular represents human judgement that would be
    annoying to lose to an absent-minded re-run.
    """
    if force:
        return
    dest = path_for(chapter_dir, stage)
    if dest.exists():
        raise ArtifactError(
            f"{dest} already exists. Re-run with --force to overwrite it "
            f"(any hand-edits to that file will be lost)."
        )
