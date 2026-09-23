"""Recall-gate tests: ada_session_recall is skipped for a window after a
confident ada_memory_search hit."""
import time

from backend.realtime_provider import GeminiLiveProvider


def _provider() -> GeminiLiveProvider:
    return GeminiLiveProvider(instructions="test")


def test_no_hit_not_gated():
    p = _provider()
    assert p._recall_gated() is False


def test_confident_hit_gates():
    p = _provider()
    p._note_search_result({"hits": [{"score": 0.75}, {"score": 0.4}]})
    assert p._recall_gated() is True


def test_low_score_does_not_gate():
    p = _provider()
    p._note_search_result({"hits": [{"score": 0.59}]})
    assert p._recall_gated() is False


def test_empty_hits_does_not_gate():
    p = _provider()
    p._note_search_result({"hits": []})
    p._note_search_result({"hits": None})
    p._note_search_result("not a dict")
    assert p._recall_gated() is False


def test_window_expires():
    p = _provider()
    p._note_search_result({"hits": [{"score": 0.9}]})
    assert p._recall_gated() is True
    p._strong_hit_at = time.monotonic() - 120
    assert p._recall_gated() is False


def test_threshold_boundary():
    p = _provider()
    p._note_search_result({"hits": [{"score": p._recall_gate_score}]})
    assert p._recall_gated() is True
