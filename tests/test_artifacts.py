"""The stage contract: what makes stages independently re-runnable."""

from __future__ import annotations

import json

import pytest

from flashcards_creator import artifacts


def test_round_trip(tmp_path):
    artifacts.write(tmp_path, "words", {"book": "b", "words": [{"lemma": "x"}]})
    doc = artifacts.read(tmp_path, "words")
    assert doc["book"] == "b"
    assert doc["stage"] == "words"
    assert doc["schema_version"] == artifacts.SCHEMA_VERSION


def test_missing_artifact_names_the_command_that_makes_it(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="fcc extract"):
        artifacts.read(tmp_path, "words")
    with pytest.raises(artifacts.ArtifactError, match="fcc level"):
        artifacts.read(tmp_path, "words_leveled")


def test_a_file_holding_the_wrong_stage_is_caught(tmp_path):
    # Each stage has its own filename, so this only happens if a file was
    # copied or renamed by hand -- but then the header is the only thing that
    # can catch it, and it should, rather than half-reading the wrong data.
    artifacts.path_for(tmp_path, "words_leveled").write_text(
        json.dumps({"schema_version": artifacts.SCHEMA_VERSION,
                    "stage": "words", "words": []}), encoding="utf-8")
    with pytest.raises(artifacts.ArtifactError, match="expected 'words_leveled'"):
        artifacts.read(tmp_path, "words_leveled")


def test_a_stale_schema_is_rejected_with_a_fix(tmp_path):
    path = artifacts.path_for(tmp_path, "words")
    path.write_text(json.dumps({"schema_version": 0, "stage": "words"}),
                    encoding="utf-8")
    with pytest.raises(artifacts.ArtifactError, match="fcc extract"):
        artifacts.read(tmp_path, "words")


def test_corrupt_json_is_reported_clearly(tmp_path):
    artifacts.path_for(tmp_path, "words").write_text("{oops", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactError, match="not valid JSON"):
        artifacts.read(tmp_path, "words")


def test_hand_edits_are_not_silently_clobbered(tmp_path):
    """A selection.json is human judgement; losing it to a re-run would hurt."""
    artifacts.write(tmp_path, "selection", {"criteria": "mine", "decisions": []})
    with pytest.raises(artifacts.ArtifactError, match="--force"):
        artifacts.guard_overwrite(tmp_path, "selection", force=False)
    artifacts.guard_overwrite(tmp_path, "selection", force=True)  # allowed


def test_a_hand_edited_selection_survives_reread(tmp_path):
    artifacts.write(tmp_path, "selection", {
        "criteria": "exclude words which seem silly",
        "decisions": [{"lemma": "basoche", "pos": "NOUN", "keep": False,
                       "reason": "medieval legal term"}],
    })
    doc = artifacts.read(tmp_path, "selection")
    assert doc["decisions"][0]["reason"] == "medieval legal term"


def test_unknown_stage_is_rejected(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match="unknown stage"):
        artifacts.path_for(tmp_path, "nonsense")
