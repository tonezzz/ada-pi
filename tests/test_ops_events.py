"""Ops-event emit: containment triggers (tool storms, actuation caps,
stripped confirms, blocked remembers) post one doc per trip to
ada-ha-events-<instance> so the chaba report feed can surface them."""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADA_INSTANCE_ID", "tony")
os.environ.setdefault("GEMINI_API_KEY", "x")

from backend.realtime_provider import GeminiLiveProvider  # noqa: E402


def _provider():
    mddb = MagicMock()
    calls = []

    async def add_document(**kw):
        calls.append(kw)

    mddb.add_document = add_document
    runner = MagicMock()
    runner.mddb = mddb
    runner.current_speaker_ha_person = None
    runner.banks.banks.return_value = {}
    p = GeminiLiveProvider(tool_runner=runner, session_id="t1")
    return p, calls


def test_emit_posts_ops_event():
    async def run():
        p, calls = _provider()
        p._emit_ops_event("tool_storm", "budget tripped", tool="control_entity")
        await asyncio.sleep(0.2)
        return calls
    calls = asyncio.run(run())
    assert len(calls) == 1
    doc = calls[0]
    assert doc["collection"] == "ada-ha-events-tony"
    assert doc["key"].startswith("ops-t1-tool_storm-")
    assert doc["meta"]["kind"] == ["ops-event"]
    assert doc["meta"]["type"] == ["tool_storm"]
    assert doc["meta"]["tool"] == ["control_entity"]
    assert doc["meta"]["session_id"] == ["t1"]


def test_emit_capped_at_five_per_session():
    async def run():
        p, calls = _provider()
        for _ in range(9):
            p._emit_ops_event("tool_storm", "x")
        await asyncio.sleep(0.2)
        return calls
    calls = asyncio.run(run())
    assert len(calls) == 5


def test_emit_disabled_without_instance():
    os.environ.pop("ADA_INSTANCE_ID", None)
    try:
        p, calls = _provider()
        p._emit_ops_event("tool_storm", "x")
        assert calls == []
    finally:
        os.environ["ADA_INSTANCE_ID"] = "tony"
