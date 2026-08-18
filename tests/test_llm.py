"""Sense-picking: response parsing, caching, and graceful degradation.

No network. The CLI's real behaviour is pinned as fixtures -- notably that it
wraps JSON in markdown fences even when told not to.
"""

from __future__ import annotations

import json

import pytest

from flashcards_creator import llm


def item(lemma="livre", pos="NOUN", sentence="Il lisait un livre.",
         candidates=("book", "pound")):
    return llm.Item(lemma=lemma, pos=pos, sentence=sentence,
                    candidates=list(candidates))


def test_strips_the_markdown_fences_the_cli_actually_emits():
    fenced = '```json\n[{"id": 0, "translation": "book"}]\n```'
    assert json.loads(llm.strip_fences(fenced))[0]["translation"] == "book"


def test_parses_a_bare_array():
    results = llm.parse_response('[{"id": 0, "translation": "book", "note": ""}]')
    assert results == [{"id": 0, "translation": "book", "note": ""}]


def test_recovers_an_array_buried_in_prose():
    noisy = 'Here you go:\n[{"id": 0, "translation": "ship"}]\nHope that helps!'
    assert llm.parse_response(noisy)[0]["translation"] == "ship"


def test_unwraps_an_object_envelope():
    assert llm.parse_response('{"items": [{"id": 1, "translation": "x"}]}')[0]["id"] == 1


def test_malformed_json_raises_rather_than_returning_junk():
    with pytest.raises(llm.LlmError):
        llm.parse_response("this is not json at all")


def test_items_without_an_id_are_discarded():
    # An id is what maps a result back to its word; without one it is unusable.
    assert llm.parse_response('[{"translation": "orphan"}]') == []


def test_cache_key_is_sensitive_to_the_sentence():
    # The same word in a different sentence may take a different sense, so it
    # must not reuse the cached answer.
    a = item(sentence="Il lisait un livre.")
    b = item(sentence="Elle acheta une livre de beurre.")
    assert a.key != b.key
    assert item().key == item().key


def test_backend_none_falls_back_to_the_first_dictionary_sense():
    results = llm.resolve_senses([item()], backend="none")
    picked = results[item().key]
    assert picked["translation"] == "book"
    assert picked["source"] == "wiktionary"


def test_backend_none_handles_words_with_no_candidates():
    empty = item(lemma="carguer", candidates=())
    picked = llm.resolve_senses([empty], backend="none")[empty.key]
    assert picked["translation"] == ""


def test_unknown_backend_is_rejected():
    with pytest.raises(llm.LlmError, match="unknown backend"):
        llm.available_backend("telepathy")


def test_conversation_backend_writes_questions_and_stops(tmp_path):
    pending = tmp_path / "pending_senses.json"
    with pytest.raises(llm.PendingSenses) as excinfo:
        llm.resolve_senses([item()], backend="conversation", pending_path=pending)

    assert excinfo.value.count == 1
    doc = json.loads(pending.read_text(encoding="utf-8"))
    entry = doc["items"][0]
    assert entry["lemma"] == "livre"
    assert entry["sentence"] == "Il lisait un livre."
    assert doc["write_answers_to"].endswith("resolved_senses.json")


def test_conversation_backend_resumes_from_written_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)

    pending = tmp_path / "pending_senses.json"
    (tmp_path / "resolved_senses.json").write_text(json.dumps({
        "answers": [{"key": item().key, "translation": "book", "note": "not 'pound'"}]
    }), encoding="utf-8")

    results = llm.resolve_senses([item()], backend="conversation", pending_path=pending)
    picked = results[item().key]
    assert picked["translation"] == "book"
    assert picked["source"] == "conversation"


def test_partial_answers_do_not_half_fill_the_deck(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    one, two = item(lemma="livre"), item(lemma="vaisseau")
    pending = tmp_path / "pending_senses.json"
    (tmp_path / "resolved_senses.json").write_text(
        json.dumps({"answers": [{"key": one.key, "translation": "book"}]}),
        encoding="utf-8")

    # Only one of two answered: ask again rather than silently guessing.
    with pytest.raises(llm.PendingSenses):
        llm.resolve_senses([one, two], backend="conversation", pending_path=pending)


def test_cached_senses_are_reused(monkeypatch):
    cached = {item().key: {"translation": "book", "note": "", "source": "claude-cli"}}
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: dict(cached))

    def explode(*a, **k):  # a cache hit must not reach a backend
        raise AssertionError("backend called despite a cache hit")

    monkeypatch.setattr(llm, "_run_claude_cli", explode)
    results = llm.resolve_senses([item()], backend="claude-cli")
    assert results[item().key]["translation"] == "book"


def test_a_failed_batch_degrades_instead_of_aborting(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_run_claude_cli",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LlmError("boom")))

    # An imperfect deck beats no deck.
    results = llm.resolve_senses([item()], backend="claude-cli")
    assert results[item().key]["translation"] == "book"


def test_a_missing_result_for_one_word_falls_back_only_for_that_word(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_run_claude_cli",
                        lambda *a, **k: '[{"id": 0, "translation": "ship"}]')

    one, two = item(lemma="vaisseau", candidates=("ship",)), item(lemma="livre")
    results = llm.resolve_senses([one, two], backend="claude-cli")
    assert results[one.key]["translation"] == "ship"
    assert results[two.key]["source"] == "wiktionary"
