#!/usr/bin/env python3
"""Live scenario driver for Ada voice sessions.

Replays scripted user text turns against a running Ada backend's /ws
endpoint and checks expectations against tool_call/tool_result trace
events and assistant transcripts.

Usage:
  python3 scripts/scenario-live.py tests/scenarios-live/michael_technician.yaml \
      --url ws://127.0.0.1:8003/ws --api-key "$ADA_API_KEY"

  --url defaults to $ADA_LIVE_URL or ws://127.0.0.1:8002/ws
  --api-key defaults to $ADA_API_KEY (matched against ADA_API_KEYS server-side)

Turn expectations (all optional, all must pass):
  calls_any: [tool, ...]       at least one of these tools was invoked
  calls: [tool, ...]           all of these tools were invoked
  no_calls: true               no tools were invoked
  result_contains: [s, ...]    each substring appears in some tool_result
  response_contains: [s, ...]  each substring appears in the spoken transcript
  response_nonempty: true      any spoken transcript at all
  timeout_s: N                 per-turn hard timeout (default 90)
  settle_s: N                  quiet period after last response (default 5)

Reconnect step (tests/scenarios-live/reconnect_continuity.yaml):
  - reconnect: {away_s: 300}   closes the ws, reconnects with
                               ?simulate_away_s=N, then checks the greeting
                               turn with the same expect vocabulary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import websockets
import yaml


def check_turn(events: list[dict], expect: dict) -> list[str]:
    failures: list[str] = []
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]
    names = {c.get("name") for c in calls}
    transcript = "".join(
        str(e.get("text") or "")
        for e in events
        if e.get("type") == "assistant_transcript_delta"
    )
    calls_any = expect.get("calls_any") or []
    if calls_any and not names & set(calls_any):
        failures.append(f"calls_any: none of {calls_any} in {sorted(names)}")
    for want in expect.get("calls") or []:
        if want not in names:
            failures.append(f"calls: {want!r} not in {sorted(names)}")
    if expect.get("no_calls") and calls:
        failures.append(f"no_calls: got {sorted(names)}")
    for sub in expect.get("result_contains") or []:
        if not any(sub in json.dumps(r.get("result") or {}, default=str) for r in results):
            failures.append(f"result_contains: {sub!r} not in any tool_result")
    for sub in expect.get("response_contains") or []:
        if sub.lower() not in transcript.lower():
            failures.append(
                f"response_contains: {sub!r} not in transcript ({transcript[:160]!r})"
            )
    if expect.get("response_nonempty") and not transcript.strip():
        failures.append("response_nonempty: empty transcript")
    return failures


async def run_turn(
    ws: Any, text: str | None, expect: dict, verbose: bool
) -> tuple[list[dict], list[str]]:
    timeout = float(expect.get("timeout_s", 90))
    settle = float(expect.get("settle_s", 5))
    events: list[dict] = []
    t0 = time.monotonic()
    if text is not None:
        await ws.send(json.dumps({"type": "text", "text": text}))
    deadline = time.monotonic() + timeout
    last_completed = 0.0
    completed = 0
    while time.monotonic() < deadline:
        # Once a response has completed, wait out the settle window for a
        # possible follow-up turn (recall results arrive as injected turns).
        wait = (
            min(settle, deadline - time.monotonic())
            if completed
            else deadline - time.monotonic()
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(wait, 0.1))
        except asyncio.TimeoutError:
            break
        if isinstance(raw, bytes):
            continue  # audio
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg["_t"] = round(time.monotonic() - t0, 3)
        events.append(msg)
        if verbose:
            t = msg.get("type")
            if t == "tool_call":
                print(f"      tool_call {msg.get('name')} {msg.get('args')}")
            elif t == "tool_result":
                print(f"      tool_result {msg.get('name')}")
            elif t in ("error", "live_reconnecting"):
                print(f"      {t}: {msg}")
        if msg.get("type") == "response_completed":
            completed += 1
            last_completed = time.monotonic()
        if completed and time.monotonic() - last_completed > settle:
            break
    return events, check_turn(events, expect)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("--url", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-t", "--timing", action="store_true",
                    help="print per-turn event timeline (send->event offsets)")
    args = ap.parse_args()

    import os

    url = args.url or os.environ.get("ADA_LIVE_URL") or "ws://127.0.0.1:8002/ws"
    api_key = args.api_key or os.environ.get("ADA_API_KEY") or ""
    if api_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={api_key}"

    async def connect(target: str) -> Any:
        ws = await websockets.connect(target, max_size=8 * 1024 * 1024)
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(raw, str) and json.loads(raw).get("type") == "ready":
                return ws

    spec = yaml.safe_load(args.scenario.read_text())
    turns = spec.get("turns") or []
    print(f"scenario: {spec.get('name') or args.scenario.stem} -> {url.split('?')[0]}")

    total_fail = 0
    ws = None
    try:
        try:
            ws = await connect(url)
        except asyncio.TimeoutError:
            print("FATAL: no ready event from server")
            return 2
        print("connected (ready)")

        for i, turn in enumerate(turns):
            expect = dict(turn.get("expect") or {})
            expect.setdefault("timeout_s", turn.get("timeout_s", 90))
            expect.setdefault("settle_s", turn.get("settle_s", 5))
            if "reconnect" in turn:
                away = (turn.get("reconnect") or {}).get("away_s")
                rurl = url
                if away is not None:
                    sep = "&" if "?" in rurl else "?"
                    rurl = f"{rurl}{sep}simulate_away_s={away}"
                print(f"turn {i + 1}: reconnect (away_s={away})")
                await ws.close()
                ws = await connect(rurl)
                # The reconnect prime triggers the greeting turn unprompted —
                # collect it like a normal response window.
                events, failures = await run_turn(ws, None, expect, args.verbose)
                text = None
            else:
                text = str(turn.get("user") or "")
                print(f"turn {i + 1}: {text!r}")
                events, failures = await run_turn(ws, text, expect, args.verbose)
            if args.timing:
                for e in events:
                    t = e.get("type")
                    if t in ("tool_call", "tool_result", "response_completed",
                             "error", "live_reconnecting"):
                        extra = f" {e.get('name')}" if e.get("name") else ""
                        print(f"      +{e.get('_t', 0):6.1f}s {t}{extra}")
            n_tools = sum(1 for e in events if e.get("type") == "tool_call")
            transcript = "".join(
                str(e.get("text") or "")
                for e in events
                if e.get("type") == "assistant_transcript_delta"
            )
            if failures:
                total_fail += len(failures)
                print(f"  FAIL ({n_tools} tool calls)")
                for f in failures:
                    print(f"    - {f}")
                print(f"    transcript: {transcript[:300]!r}")
            else:
                print(f"  ok ({n_tools} tool calls) ada: {transcript[:140]!r}")
    finally:
        if ws is not None:
            await ws.close()

    print(f"{'PASS' if total_fail == 0 else 'FAIL'}: {total_fail} failed expectations")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
