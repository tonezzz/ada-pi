#!/usr/bin/env python3
"""Mine Ada session transcripts for confirmation turns — backfills the
Jev corpus with real labeled data instead of waiting for live traffic.

Logic mirrors realtime_provider._user_confirmed: a User turn counts as
an affirmation only when the WHOLE turn is short (<=60 chars) and matches
the affirmation regex, or the regex hits within the first 20 chars of a
longer turn. Same rules = the mined labels match what the gate would do.

A User turn is mined when the preceding Ada turn asked for confirmation
(patterns: ยืนยัน, confirm, "shall I", "...ไหมครับ" after proposing an
action, etc.). Rows carry src=transcript so they can be filtered or
excluded from training separately from live-probe rows.

Usage:
  python3 scripts/transcript-mine-confirms.py [transcript_dir]
      [--out PATH]  (default ~/.local/share/ada/jev-corpus-mined.jsonl)
      [--score-jev URL]  (optionally score each turn with Jev — SLOW,
                          ~12s/row; run on a subset)
      [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

# Copied verbatim from backend/realtime_provider.py — keep in sync.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|sure|okay?|"
    r"approved?|proceed|absolutely|mhm|uh huh|sounds good)\b|"
    r"ใช่|ยืนยัน|ตกลง|เอาเลย|ทำเลย|ได้เลย|ทำได้|โอเค|ออเค|เออ|อือ|"
    r"ต่อไป|จัดไป|เอาสิ|ไปเลย|ทำไป|เผยแพร่เลย|ส่งเลย",
    re.IGNORECASE,
)
_CONFIRM_LEAD_WINDOW = 20
_CONFIRM_MAX_TURN = 60

# Ada turns that constitute a confirmation ask — bilingual.
_ASK_RE = re.compile(
    r"ยืนยัน|ยีนยัน|โอเคไหม|โอเคมั้ย|เอาไหม|ไหมครับ|ไหมคะ|"
    r"เห็นด้วย|ตกลงไหม|พร้อมไหม|จะให้.{0,20}ไหม|"
    r"confirm|shall I|want me to|sound good|okay to|ok to|proceed\?",
    re.IGNORECASE,
)

_TURN_RE = re.compile(r"^## (Ada|User) \[([^\]]+)\]\s*$")


def _regex_says(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if len(t) <= _CONFIRM_MAX_TURN:
        return bool(_CONFIRM_RE.search(t))
    return bool(_CONFIRM_RE.search(t[:_CONFIRM_LEAD_WINDOW]))


def _parse_turns(path: Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    role, buf = None, []
    def flush():
        if role and buf:
            turns.append((role, " ".join(buf).strip()))
        buf.clear()
    for line in path.read_text(errors="replace").splitlines():
        m = _TURN_RE.match(line)
        if m:
            flush()
            role = m.group(1)
            continue
        if role:
            buf.append(line.strip())
    flush()
    return turns


def _score_jev(url: str, text: str, timeout: float = 45.0) -> float | None:
    payload = {
        "state": (
            "Ada, a voice assistant, asked the user to confirm a "
            f"write/action. The user turn was: \"{text[:300]}\""),
        "questions": {"affirmed": {
            "type": "noul",
            "instructions": (
                "Did the user explicitly affirm? Standalone short "
                "affirmation or leading affirmation counts; an approval "
                "word embedded inside a longer request does NOT."),
            "criteria": {
                "true": "the whole turn is a short affirmation, or it leads with one",
                "false": "no affirmation, or an affirmative word buried in a longer request",
            }}}}
    try:
        req = urllib.request.Request(
            f"{url}/v1/systemone", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return float(json.loads(urllib.request.urlopen(
            req, timeout=timeout).read())["answers"]["affirmed"]["noul"])
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript_dir", nargs="?",
                    default=str(Path.home() / ".local/share/ada/transcripts"))
    ap.add_argument("--out", default=str(
        Path.home() / ".local/share/ada/jev-corpus-mined.jsonl"))
    ap.add_argument("--score-jev", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out_rows: list[dict] = []
    for path in sorted(Path(args.transcript_dir).glob("*.md")):
        turns = _parse_turns(path)
        for i in range(1, len(turns)):
            if turns[i][0] != "User" or turns[i - 1][0] != "Ada":
                continue
            ask, reply = turns[i - 1][1], turns[i][1]
            if not ask or not reply or not _ASK_RE.search(ask):
                continue
            out_rows.append({
                "text": reply, "regex": _regex_says(reply),
                "tool": "mined", "src": f"{path.name}:{i}",
                "ask": ask[:200],
            })
            if args.limit and len(out_rows) >= args.limit:
                break
        if args.limit and len(out_rows) >= args.limit:
            break

    scored = 0
    if args.score_jev:
        for r in out_rows:
            r["jev"] = _score_jev(args.score_jev, r["text"])
            if r["jev"] is not None:
                scored += 1
                r["diverged"] = (r["jev"] >= 0.75) != r["regex"]

    Path(args.out).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_rows))
    pos = sum(1 for r in out_rows if r["regex"])
    div = sum(1 for r in out_rows if r.get("diverged"))
    print(json.dumps({
        "mined": len(out_rows), "affirm": pos, "non_affirm": len(out_rows) - pos,
        "jev_scored": scored, "diverged": div, "out": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
