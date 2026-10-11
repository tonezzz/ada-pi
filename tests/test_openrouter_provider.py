"""OpenRouterTextProvider — degraded text fallback (card ada-chat-or-fallback)."""
import asyncio
import os

import pytest

from backend.openrouter_provider import OpenRouterTextProvider, _is_context_turn


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return OpenRouterTextProvider(session_id="t1", channel="telegram")


def test_context_turns_recognized():
    assert _is_context_turn("(system) Guest context follows.")
    assert _is_context_turn("Session note: the user's connection dropped")
    assert _is_context_turn("Recent sessions: talked about lights\n(system) x")
    assert not _is_context_turn("turn on the kitchen light")
    assert not _is_context_turn("hello")


@pytest.mark.asyncio
async def test_connect_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    p = OpenRouterTextProvider(session_id="t2", channel="telegram")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await p.connect()


@pytest.mark.asyncio
async def test_context_turn_does_not_call_api(provider, monkeypatch):
    calls = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "hi"}}]}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            calls.append((a, kw))
            return FakeResp()

    import backend.openrouter_provider as m
    monkeypatch.setattr(m.httpx, "AsyncClient", FakeClient)
    await provider.send_text_turn("(system) Guest context follows.")
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_user_turn_emits_reply_events(provider, monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "Hello Tony!"}}]}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return FakeResp()

    import backend.openrouter_provider as m
    monkeypatch.setattr(m.httpx, "AsyncClient", FakeClient)
    await provider.send_text_turn("hi ada")
    events = []
    async for ev in provider.events():
        events.append(ev.type)
        if ev.type == "response_completed":
            break
    assert events[0] == "response_started"
    assert "assistant_transcript_delta" in events
    assert events[-1] == "response_completed"
    # Reply text reassembled from deltas
    text = ""
    # re-walk history instead — deltas already consumed
    assert provider._history[-1] == {"role": "assistant",
                                     "content": "Hello Tony!"}


@pytest.mark.asyncio
async def test_api_error_surfaces_interruption(provider, monkeypatch):
    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise RuntimeError("401 boom")

    import backend.openrouter_provider as m
    monkeypatch.setattr(m.httpx, "AsyncClient", FakeClient)
    await provider.send_text_turn("hi ada")
    async for ev in provider.events():
        if ev.type == "response_interrupted":
            assert "openrouter" in ev.data.get("reason", "")
            break
