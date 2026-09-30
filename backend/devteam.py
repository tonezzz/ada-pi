#!/usr/bin/env python3
"""Devteam pipeline — Ada-drafted tool spec -> parallel expert review ->
revised spec with a GO/BLOCK verdict.

Three bounded stages, each a genai call:
  1. draft   — turn a plain-language request into a tool spec.
  2. panel   — expert reviewers run in parallel; each returns a verdict
               (pass | warn | block) plus comments.
  3. revise  — the spec is updated per panel feedback; overall verdict is
               BLOCK if any expert blocked.

The result is meant to land in the devin-handoff bank (kind=spec-review)
where the dispatch pipeline can pick it up. See docs/ada-tool-dev.md for
the authoring contract the standards expert enforces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

DEVTEAM_MODEL = os.environ.get("DEVTEAM_MODEL", "gemini-2.5-flash")
DEVTEAM_TIMEOUT_S = float(os.environ.get("DEVTEAM_TIMEOUT_S", "90"))
_PANEL_TIMEOUT_S = float(os.environ.get("DEVTEAM_PANEL_TIMEOUT_S", "60"))

TOOL_CONTRACT = """\
The tool must follow the tools.d contract (docs/ada-tool-dev.md):
- module in backend/tools.d/ with DECLARATION (name, description, parameters)
  and async run(runner, **args) -> dict
- manifest.yml entry: policy (read|confirmed|owner_only), timeout_s
- honest results: report what actually happened, never claim a write that
  did not occur; missing dependencies -> structured error, not silent ok
"""

EXPERTS: dict[str, str] = {
    "security_privacy": (
        "You are the security & privacy reviewer for Ada's tool pipeline. "
        "Check: does this tool expose private memory banks or data to the "
        "wrong identity? Does it need owner_only/confirmed policy? Could "
        "it leak secrets, bypass speaker gates, or act on unverified input? "
        "Reject anything that reads Tony-only banks without an owner gate."
    ),
    "standards": (
        "You are the standards reviewer. Enforce the tools.d authoring "
        "contract and repo conventions: honest results, bounded timeouts, "
        "no core-file edits for the tool itself, naming is ada_* or "
        "domain-prefixed, parameters validated defensively. Reject specs "
        "that require editing tool_runner.py or realtime_provider.py."
    ),
    "qa": (
        "You are the QA reviewer. Define the smallest meaningful test plan: "
        "at least one unit test (mocked runner/genai) and, if the tool is "
        "user-facing, a live-scenario expectation. Reject specs with no "
        "verifiable success signal."
    ),
    "scope": (
        "You are the scope reviewer. Cut the spec to the smallest thing "
        "that works: no extra parameters, no speculative features, no "
        "premature config. Reject specs that bundle unrelated work."
    ),
}

_VERDICT_ORDER = {"pass": 0, "warn": 1, "block": 2}


def _json_hint(extra: str = "") -> str:
    return (
        'Reply with ONLY a JSON object, no prose: {"verdict": "pass"|"warn"|'
        '"block", "summary": "one line", "comments": ["..."]}' + extra
    )


class DevTeam:
    """Requirement -> panel -> revised spec. Client injectable for tests."""

    def __init__(self, client: Any = None) -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY")
        self.model = os.environ.get("DEVTEAM_MODEL", DEVTEAM_MODEL)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def _generate(self, prompt: str, timeout: float) -> str:
        """One bounded call with bounded 429 retry — same quota-sharing
        constraints as decision_check."""
        for attempt in range(3):
            try:
                resp = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=[types.Part.from_text(text=prompt)],
                        config=types.GenerateContentConfig(
                            automatic_function_calling=(
                                types.AutomaticFunctionCallingConfig(
                                    disable=True)),
                        ),
                    ),
                    timeout=timeout,
                )
                return resp.text or ""
            except Exception as exc:
                if "429" not in str(exc) or attempt == 2:
                    raise
                match = re.search(
                    r"retry in ([\d.]+)s|retryDelay.*?(\d+)s", str(exc))
                delay = min(45.0, float(
                    next(g for g in match.groups() if g)) + 2) if match else 10.0
                logger.info("devteam: 429 quota, retry in %.0fs", delay)
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in response")
        return json.loads(match.group(0))

    async def draft_spec(self, request: str) -> str:
        prompt = (
            "You are the requirements engineer for Ada's tool pipeline. A "
            "voice request asked for a new tool. Draft a concise tool spec "
            "in markdown: name (ada_* or domain prefix), purpose in one "
            "sentence, parameters, policy class (read|confirmed|owner_only "
            "+ secondary_allowed?), expected result shape, and a one-line "
            "honest-failure behavior.\n\n" + TOOL_CONTRACT +
            "\nRequest:\n" + request.strip()[:2000]
        )
        return await self._generate(prompt, DEVTEAM_TIMEOUT_S)

    async def _reviewer(self, role: str, spec: str) -> dict[str, Any]:
        prompt = (
            EXPERTS[role] + "\n\n" + TOOL_CONTRACT +
            "\nSpec under review:\n" + spec[:6000] + "\n\n" + _json_hint()
        )
        try:
            text = await self._generate(prompt, _PANEL_TIMEOUT_S)
            v = self._extract_json(text)
            verdict = str(v.get("verdict") or "warn").lower()
            if verdict not in _VERDICT_ORDER:
                verdict = "warn"
            return {
                "role": role, "verdict": verdict,
                "summary": str(v.get("summary") or "")[:300],
                "comments": [str(c)[:300] for c in
                             (v.get("comments") or [])][:6],
            }
        except Exception as exc:
            logger.info("devteam panel %s failed: %s", role, exc)
            return {"role": role, "verdict": "warn",
                    "summary": f"reviewer unavailable: {exc}",
                    "comments": []}

    async def run_panel(self, spec: str) -> list[dict[str, Any]]:
        return list(await asyncio.gather(
            *(self._reviewer(r, spec) for r in EXPERTS)))

    async def revise(self, spec: str, panel: list[dict[str, Any]]) -> str:
        feedback = "\n".join(
            f"- [{r['role']} / {r['verdict']}] {r['summary']} " +
            "; ".join(r["comments"])
            for r in panel if r["verdict"] != "pass" or r["comments"])
        prompt = (
            "You are the requirements engineer revising a tool spec after "
            "expert review. Apply the feedback below — resolve every block "
            "and warn if you can; if a block is fundamental, keep the spec "
            "but mark it NOT READY. Output the full revised spec in "
            "markdown.\n\nSpec:\n" + spec[:6000] + "\n\nFeedback:\n" +
            (feedback or "- (no feedback)")[:4000]
        )
        return await self._generate(prompt, DEVTEAM_TIMEOUT_S)

    async def review(self, request: str) -> dict[str, Any]:
        """Full pipeline. Returns draft, panel, revised spec, verdict."""
        spec = await self.draft_spec(request)
        panel = await self.run_panel(spec)
        revised = await self.revise(spec, panel)
        overall = "block" if any(r["verdict"] == "block" for r in panel) \
            else ("warn" if any(r["verdict"] == "warn" for r in panel)
                  else "pass")
        return {
            "ok": True,
            "verdict": overall,
            "spec": revised,
            "draft": spec,
            "panel": panel,
            "model": self.model,
        }


async def review(request: str, client: Any = None) -> dict[str, Any]:
    return await DevTeam(client).review(request)
