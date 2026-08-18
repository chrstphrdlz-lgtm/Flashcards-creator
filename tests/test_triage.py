"""Triage: Claude's keep/drop judgement on each word.

No network. The model's actual judgement was validated separately against
known cases; these tests pin the plumbing around it -- especially the failure
behaviour, because the dangerous failure mode is losing vocabulary silently.
"""

from __future__ import annotations

from flashcards_creator import llm, triage


def candidate(lemma="basoche", pos="NOUN", level="C1", freq=0.07, count=1):
    return triage.Candidate(
        lemma=lemma, pos=pos, level=level, modern_freq=freq, count=count,
        sentence=f"Un exemple avec {lemma} dedans.")


def test_cache_key_is_namespaced_away_from_sense_picking():
    # Both live in one cache file; a collision would feed a translation to the
    # keep/drop logic or vice versa.
    key = candidate().key
    assert key.startswith("triage:")
    assert key != llm.Item("basoche", "NOUN", "s", []).key


def test_payload_carries_the_evidence_needed_to_judge():
    payload = candidate(count=40).payload(3)
    assert payload["id"] == 3
    # Frequency alone cannot separate arcane from useful, so recurrence and
    # level have to travel with it.
    for field in ("lemma", "pos", "level", "modern_freq", "count", "sentence"):
        assert field in payload
    assert payload["count"] == 40


def test_from_row_reads_a_leveled_word():
    row = {"lemma": "surcot", "pos": "NOUN", "level": "C2",
           "modern_freq": 0.14, "count": 2, "sentence": "Il portait un surcot."}
    assert triage.from_row(row).lemma == "surcot"
    assert triage.from_row(row).count == 2


def test_verdicts_are_applied_to_the_selection(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        llm, "_run_claude_cli",
        lambda *a, **k: '[{"id":0,"keep":false,"reason":"medieval legal term"},'
                        ' {"id":1,"keep":true,"reason":"current in modern French"}]')

    rows = [
        {"lemma": "basoche", "pos": "NOUN", "level": "C1",
         "modern_freq": 0.07, "count": 1, "sentence": "…"},
        {"lemma": "ruisseler", "pos": "VERB", "level": "C2",
         "modern_freq": 19.1, "count": 3, "sentence": "…"},
    ]
    verdicts = triage.judge([triage.from_row(r) for r in rows],
                            backend="claude-cli", workers=1)
    selection = triage.build_selection(rows, verdicts, "exclude arcane vocabulary")

    by_lemma = {d["lemma"]: d for d in selection["decisions"]}
    assert by_lemma["basoche"]["keep"] is False
    assert by_lemma["ruisseler"]["keep"] is True
    assert selection["criteria"] == "exclude arcane vocabulary"


def test_every_decision_carries_a_reason(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_run_claude_cli",
                        lambda *a, **k: '[{"id":0,"keep":false,"reason":"archaic"}]')

    rows = [{"lemma": "sotie", "pos": "NOUN", "level": "C2",
             "modern_freq": 0.07, "count": 1, "sentence": "…"}]
    verdicts = triage.judge([triage.from_row(r) for r in rows], backend="claude-cli")
    for decision in triage.build_selection(rows, verdicts, "x")["decisions"]:
        assert decision["reason"], "a word was dropped with no reason given"


def test_a_failed_batch_keeps_its_words(monkeypatch):
    """Losing vocabulary to a transport error is worse than an unjudged card."""
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_run_claude_cli",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LlmError("boom")))

    verdicts = triage.judge([candidate()], backend="claude-cli", workers=1)
    verdict = verdicts[candidate().key]
    assert verdict["keep"] is True
    assert verdict["source"] == "error"
    assert "fail" in verdict["reason"].lower()


def test_a_missing_verdict_keeps_that_word(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    monkeypatch.setattr(llm, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_run_claude_cli", lambda *a, **k: "[]")

    verdict = triage.judge([candidate()], backend="claude-cli")[candidate().key]
    assert verdict["keep"] is True


def test_without_a_backend_nothing_is_dropped(monkeypatch):
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: {})
    verdicts = triage.judge([candidate()], backend="none")
    assert verdicts[candidate().key]["keep"] is True
    # The totals must make it obvious that no judging happened.
    assert "not judged" in verdicts[candidate().key]["reason"]


def test_cached_verdicts_skip_the_backend(monkeypatch):
    cached = {candidate().key: {"keep": False, "reason": "archaic", "source": "claude-cli"}}
    monkeypatch.setattr(llm, "load_cache", lambda *a, **k: dict(cached))

    def explode(*a, **k):
        raise AssertionError("backend called despite a cache hit")

    monkeypatch.setattr(llm, "_run_claude_cli", explode)
    assert triage.judge([candidate()], backend="claude-cli")[candidate().key]["keep"] is False


def test_summary_reports_what_was_cut():
    decisions = [
        {"lemma": "basoche", "keep": False, "reason": "archaic",
         "level": "C1", "modern_freq": 0.07},
        {"lemma": "surcot", "keep": False, "reason": "obsolete garment",
         "level": "C2", "modern_freq": 0.14},
        {"lemma": "ruisseler", "keep": True, "reason": "current",
         "level": "C2", "modern_freq": 19.1},
    ]
    summary = triage.summarise(decisions)
    assert summary["total"] == 3
    assert summary["kept"] == 1 and summary["dropped"] == 2
    assert summary["kept_by_level"]["C2"] == 1
    # Rarest first, so an over-deep cut is visible at a glance.
    assert summary["sample_dropped"][0]["lemma"] == "basoche"
