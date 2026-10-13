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
    assert provider._turns[-1] == {"role": "model",
                                   "parts": [{"text": "Hello Tony!"}]}


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
            assert "401 boom" in ev.data.get("reason", "")
            break


@pytest.mark.asyncio
async def test_text_mode_tool_loop(monkeypatch):
    """tools=True: gemini REST turn executes tool calls via tool_runner and
    emits tool_call/tool_result frames (card ada-text-mode-sessions)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeFC:
        def __init__(self, name, args):
            self.name, self.args, self.id = name, args, None

    class FakePart:
        def __init__(self, text=None, fc=None):
            self.text, self.function_call = text, fc

    class FakeCand:
        def __init__(self, parts):
            self.content = type("C", (), {"parts": parts})()

    class FakeResp:
        def __init__(self, parts):
            self.candidates = [FakeCand(parts)]

    calls = []

    async def fake_generate(model, contents, config=None, **kw):
        if not calls:
            return FakeResp([FakePart(fc=FakeFC("fake_tool", {"x": 1}))])
        return FakeResp([FakePart(text="done")])

    import backend.openrouter_provider as m
    monkeypatch.setattr(m.gemini_pool, "generate", fake_generate)

    class FakeTR:
        current_speaker_ha_person = None
        session_owner_identity = None
        event_log = None
        speaker_session = None

        async def execute(self, name, args, **kw):
            calls.append((name, args, kw))
            return {"ok": True, "echo": args}

    # Harvest shortcut: fake _session_config on the probe.
    from backend.realtime_provider import GeminiLiveProvider

    async def fake_cfg(self, resumption_handle=None):
        return {"tools": [{"function_declarations": [
                    {"name": "fake_tool",
                     "parameters_json_schema": {"type": "object"}}]}],
                "system_instruction": "SYS"}

    monkeypatch.setattr(GeminiLiveProvider, "_session_config", fake_cfg)

    provider = OpenRouterTextProvider(
        session_id="t9", channel="line", tools=True,
        tool_runner=FakeTR(), caller_name="tester",
        caller_person="tester")
    await provider.connect()
    assert provider._gemini_decls == [
        {"name": "fake_tool",
         "parameters_json_schema": {"type": "object"}}]
    await provider.send_text_turn("turn on the light")
    events = []
    async for ev in provider.events():
        events.append(ev.type)
        if ev.type == "response_completed":
            break
    assert "tool_call" in events
    assert "tool_result" in events
    assert calls and calls[0][0] == "fake_tool"
    assert calls[0][2]["owner"] == "tester"
    # tool result round-tripped into history as a function_response part
    assert any(p.get("function_response", {}).get("name") == "fake_tool"
               for t in provider._turns if t["role"] == "tool"
               for p in t["parts"])


@pytest.mark.asyncio
async def test_text_mode_image_turn(monkeypatch):
    """tools=True: image turns keep the bytes inline (not caption-only)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    provider = OpenRouterTextProvider(session_id="t10", channel="line",
                                      tools=True)
    provider._pending = asyncio.create_task(asyncio.sleep(60))
    await provider.send_image_turn(b"\xff\xd8img", "image/jpeg", "a map")
    provider._pending.cancel()
    last = provider._turns[-1]
    assert last["role"] == "user"
    assert last["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"
    assert last["parts"][1]["text"] == "a map"
