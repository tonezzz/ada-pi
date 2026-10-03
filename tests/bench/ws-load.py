#!/usr/bin/env python3
"""Concurrent WS load probe for Ada — ramps N simultaneous sessions, each
sending a text turn, and measures per-turn latency to response_completed.

Usage:
  python3 ws-load.py ws://127.0.0.1:8012/ws --api-key KEY --ramp 1,2,4,8
"""
import argparse, asyncio, json, statistics, time
import websockets

PROMPTS = [
    "what time is it",
    "list my virtual screens",
    "what's the weather like",
    "tell me a one-sentence fact",
]


async def one_turn(url: str, api_key: str, prompt: str, timeout: float) -> dict:
    t0 = time.monotonic()
    try:
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}api_key={api_key}" if api_key else url
        async with websockets.connect(full, max_size=8 * 1024 * 1024) as ws:
            t_conn = time.monotonic() - t0
            # wait for ready/hello
            ready_at = None
            async def wait_ready():
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    m = json.loads(raw)
                    if m.get("type") in ("ready", "session_ready", "hello"):
                        return
                    if m.get("type") == "error":
                        raise RuntimeError(str(m)[:200])
            try:
                await asyncio.wait_for(wait_ready(), timeout=10)
            except asyncio.TimeoutError:
                pass  # some servers send no ready — proceed
            ready_at = time.monotonic() - t0

            t_send = time.monotonic()
            await ws.send(json.dumps({"type": "text", "text": prompt}))
            first_text_at = done_at = None
            deadline = t_send + timeout
            async for raw in ws:
                if time.monotonic() > deadline:
                    break
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                t = m.get("type")
                if t in ("transcript", "text", "assistant_text") and first_text_at is None:
                    first_text_at = time.monotonic() - t_send
                if t == "response_completed":
                    done_at = time.monotonic() - t_send
                    break
            return {"ok": done_at is not None, "connect_ms": int(t_conn * 1000),
                    "first_text_ms": int((first_text_at or -1) * 1000),
                    "turn_ms": int((done_at or -1) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160],
                "ms": int((time.monotonic() - t0) * 1000)}


async def ramp_level(url: str, api_key: str, n: int, timeout: float) -> dict:
    tasks = [one_turn(url, api_key, PROMPTS[i % len(PROMPTS)], timeout)
             for i in range(n)]
    t0 = time.monotonic()
    results = await asyncio.gather(*tasks)
    wall = time.monotonic() - t0
    oks = [r for r in results if r.get("ok")]
    turn_ms = [r["turn_ms"] for r in oks if r.get("turn_ms", -1) > 0]
    first_ms = [r["first_text_ms"] for r in oks if r.get("first_text_ms", -1) > 0]
    errs = [r.get("error") for r in results if not r.get("ok")]
    return {"n": n, "ok": len(oks), "fail": n - len(oks),
            "wall_s": round(wall, 1),
            "turn_ms_med": int(statistics.median(turn_ms)) if turn_ms else -1,
            "turn_ms_p90": int(statistics.quantiles(turn_ms, n=10)[8]) if len(turn_ms) >= 10 else (max(turn_ms) if turn_ms else -1),
            "first_ms_med": int(statistics.median(first_ms)) if first_ms else -1,
            "errors": errs[:3]}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--ramp", default="1,2,4,8")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = []
    for n in [int(x) for x in args.ramp.split(",")]:
        r = await ramp_level(args.url, args.api_key, n, args.timeout)
        rows.append(r)
        print(f"N={r['n']:>3}  ok={r['ok']}/{r['n']}  "
              f"turn_med={r['turn_ms_med']}ms  p90={r['turn_ms_p90']}ms  "
              f"first_text_med={r['first_ms_med']}ms  wall={r['wall_s']}s"
              + (f"  errs={r['errors']}" if r["errors"] else ""), flush=True)
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
