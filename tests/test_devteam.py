"""Unit tests for backend/devteam.py — the spec-review pipeline.

The genai client is faked by overriding _generate, so no network access.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import devteam  # noqa: E402


class FakeDevTeam(devteam.DevTeam):
    """DevTeam with _generate routed to canned responses by prompt kind."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        super().__init__(client=object())  # never touched
        self.responses = responses or {}
        self.prompts: list[str] = []

    async def _generate(self, prompt: str, timeout: float) -> str:
        self.prompts.append(prompt)
        for key, resp in self.responses.items():
            if key in prompt:
                return resp
        # panel default: pass verdict
        if "Reply with ONLY a JSON object" in prompt:
            return json.dumps({"verdict": "pass", "summary": "looks fine",
                               "comments": []})
        return "# spec draft\n\nname: ada_test_tool"


def _run(coro):
    return asyncio.run(coro)


def test_draft_spec_mentions_contract():
    t = FakeDevTeam()
    spec = _run(t.draft_spec("a tool that counts devin sessions"))
    assert "ada_test_tool" in spec
    assert "tools.d" in t.prompts[0]


def test_panel_runs_all_experts_in_parallel():
    t = FakeDevTeam()
    panel = _run(t.run_panel("# spec"))
    assert {r["role"] for r in panel} == set(devteam.EXPERTS)
    assert all(r["verdict"] == "pass" for r in panel)


def test_block_verdict_propagates():
    t = FakeDevTeam(responses={
        "security & privacy reviewer": json.dumps(
            {"verdict": "block", "summary": "leaks tony bank",
             "comments": ["missing owner gate"]}),
    })
    result = _run(t.review("a tool that reads the personal bank"))
    assert result["ok"] is True
    assert result["verdict"] == "block"
    sec = next(r for r in result["panel"]
               if r["role"] == "security_privacy")
    assert sec["verdict"] == "block"


def test_warn_when_reviewer_unavailable():
    t = FakeDevTeam(responses={
        "QA reviewer": "definitely not json",
    })
    panel = _run(t.run_panel("# spec"))
    qa = next(r for r in panel if r["role"] == "qa")
    assert qa["verdict"] == "warn"
    assert "unavailable" in qa["summary"]


def test_extract_json_strips_prose():
    v = devteam.DevTeam._extract_json(
        'Here you go:\n```json\n{"verdict": "warn", "summary": "x"}\n```')
    assert v["verdict"] == "warn"


def test_review_returns_revised_spec():
    t = FakeDevTeam(responses={"revising": "# revised spec v2"})
    result = _run(t.review("design a speaker-stats tool"))
    assert result["verdict"] == "pass"
    assert result["spec"] == "# revised spec v2"
    assert result["model"] == t.model
