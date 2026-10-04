#!/usr/bin/env python3
"""Tool-usage census — scan the service journal for tool calls and keep
the CMS page `report/tool-usage` live. Runs from ada-tool-usage.timer
(daily) on idc01; safe to run by hand anywhere the journal is readable.

Counts per-tool calls from tool_runner's `tool <name> args=…` log lines
plus consolidation-alias hits (`tool alias <old> -> <new>`) and gate
denials (`denied <name>: …`), then publishes a markdown report to MDDB
ada-cms-pages so Ada can answer "which tools are actually used?" from
one read — the live counterpart of docs/assessments/tool-surface-*.

Usage:
  tool-usage-report.py [--since "-7d"] [--unit ada-ha-tony ...]
                       [--tools-url http://127.0.0.1:8002/api/tools]
                       [--journal-file PATH] [--dry-run]

Env: MDDB_BASE_URL (default http://127.0.0.1:11023/v1),
     ADA_API_KEY (for /api/tools), ADA_TOOL_USAGE_UNITS
     (default "ada-ha-tony ada-ha-michael ada-dev").
Exit: 0 ok / 1 publish failed / 2 nothing to report.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
PAGE_KEY = "report/tool-usage"
RE_CALL = re.compile(r"^tool ([a-z0-9_]+) args=")
RE_ALIAS = re.compile(r"^tool alias ([a-z0-9_]+) -> ([a-z0-9_]+)")
RE_DENIED = re.compile(r"^denied ([a-z0-9_]+):")
RE_NORM = re.compile(r"^tool name normalized (\S+) -> ([a-z0-9_]+)")

PASS = object()


def _load_lint():
    """Reuse tool-lint.py's static census (scripts/ isn't a package)."""
    spec = importlib.util.spec_from_file_location(
        "tool_lint", REPO / "scripts/tool-lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live_declared(url: str, api_key: str) -> set[str] | None:
    """Tool names the running backend offers right now (/api/tools)."""
    try:
        req = urllib.request.Request(
            url, headers={"x-api-key": api_key} if api_key else {})
        return set(json.loads(
            urllib.request.urlopen(req, timeout=10).read()).get("tools") or [])
    except Exception as exc:
        print(f"tools-url unreachable ({exc}) — falling back to static census",
              file=sys.stderr)
        return None


def static_declared() -> set[str]:
    try:
        return set(_load_lint().collect_surface(REPO)["declared"])
    except Exception as exc:
        print(f"static census failed ({exc})", file=sys.stderr)
        return set()


def journal_lines(units: list[str], since: str) -> list[str]:
    """MESSAGE fields from journalctl for the given units."""
    out: list[str] = []
    cmd = ["journalctl", "-o", "json", "--no-pager",
           "--output-fields", "MESSAGE", "--since", since]
    for scope in (["--user"], []):
        got_any = False
        for unit in units:
            try:
                raw = subprocess.check_output(
                    cmd[:1] + scope + cmd[1:] + ["-u", unit],
                    text=True, timeout=60, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    OSError):
                continue
            for line in raw.splitlines():
                try:
                    msg = json.loads(line).get("MESSAGE")
                except ValueError:
                    continue
                if isinstance(msg, str):
                    out.append(msg)
                    got_any = True
        if got_any:
            break   # --user found data; don't double-count the system view
    return out


def scan(messages: list[str], declared: set[str], since_days: int) -> dict:
    calls: dict[str, int] = collections.Counter()
    denied: dict[str, int] = collections.Counter()
    alias_hits: dict[str, int] = collections.Counter()
    normalized: dict[str, int] = collections.Counter()
    for msg in messages:
        m = RE_CALL.match(msg)
        if m:
            calls[m.group(1)] += 1
            continue
        m = RE_ALIAS.match(msg)
        if m:
            alias_hits[m.group(1)] += 1
            calls[m.group(2)] += 1          # alias lands on canonical
            continue
        m = RE_DENIED.match(msg)
        if m:
            denied[m.group(1)] += 1
            continue
        m = RE_NORM.match(msg)
        if m:
            normalized[m.group(2)] += 1
    return {
        "calls": calls, "denied": denied, "alias_hits": alias_hits,
        "normalized": normalized,
        "unknown": {t: n for t, n in calls.items() if t not in declared},
        "zero_use": sorted(t for t in declared if t not in calls),
        "since_days": since_days,
    }


def render(stats: dict, declared: set[str], units: list[str],
           now: datetime) -> str:
    calls, denied = stats["calls"], stats["denied"]
    days = stats["since_days"]
    lines = [
        f"# Tool usage — {now:%Y-%m-%d %H:%M} (last {days}d)", "",
        f"{len(declared)} tools declared · {len(calls)} saw calls · "
        f"{sum(calls.values())} total calls · "
        f"{sum(denied.values())} gate denials", "",
        "## Calls by tool", "",
        "| tool | calls | denied | note |", "|---|---|---|---|",
    ]
    for name in sorted(declared | set(calls),
                       key=lambda t: (-calls.get(t, 0), t)):
        n = calls.get(name, 0)
        note = []
        if name not in declared:
            note.append("**not declared**")
        if n == 0 and name in declared:
            note.append("zero-use")
        if stats["alias_hits"].get(name):
            note.append(f"alias for {stats['alias_hits'][name]} call(s)")
        if stats["normalized"].get(name):
            note.append(f"{stats['normalized'][name]} phonetic-normalized")
        lines.append(f"| {name} | {n} | {denied.get(name, 0)} "
                     f"| {'; '.join(note)} |")
    if stats["alias_hits"]:
        lines += ["", "## Alias hits (absorbed names still called)", ""]
        for old, n in sorted(stats["alias_hits"].items(),
                             key=lambda kv: -kv[1]):
            lines.append(f"- `{old}` — {n} call(s); decay to ~0 = "
                         "removable alias row")
    if stats["unknown"]:
        lines += ["", "## Calls to undeclared names", ""]
        for t, n in sorted(stats["unknown"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{t}` — {n} call(s) (stale prompt habit, "
                         "removed tool, or typo drift)")
    lines += ["", f"---", f"journal units: {', '.join(units)} — "
              "refreshed by ada-tool-usage.timer"]
    return "\n".join(lines)


def publish(mddb: str, content_md: str, stats: dict, now: datetime) -> bool:
    meta = {
        "kind": ["report"], "domain": ["tools"], "suite": ["tool-usage"],
        "slug": [PAGE_KEY],
        "title": [f"Tool usage — {now:%Y-%m-%d}"],
        "format": ["markdown"], "instance": ["idc01"],
        "updated": [now.isoformat(timespec="seconds")],
        "fresh_for": ["25h"],
        "summary": [f"{len(stats['calls'])} tools called, "
                    f"{len(stats['zero_use'])} zero-use, "
                    f"{len(stats['unknown'])} undeclared"],
        "timeline": [f"{now.isoformat(timespec='minutes')}: census "
                     f"{len(stats['calls'])}/{len(stats['calls']) + len(stats['zero_use'])} tools active"],
    }
    req = urllib.request.Request(
        mddb.rstrip("/") + "/add",
        data=json.dumps({
            "collection": CMS_COLLECTION, "key": PAGE_KEY, "lang": "en",
            "contentMd": content_md, "meta": meta}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="-7d",
                    help="journalctl --since value (default -7d)")
    ap.add_argument("--days", type=int, default=7,
                    help="window label for the report header")
    ap.add_argument("--unit", action="append", dest="units")
    ap.add_argument("--tools-url", default=os.environ.get(
        "ADA_TOOLS_URL", "http://127.0.0.1:8002/api/tools"))
    ap.add_argument("--api-key", default=os.environ.get("ADA_API_KEY", ""))
    ap.add_argument("--journal-file",
                    help="parse a file of journal lines instead of journalctl")
    ap.add_argument("--mddb", default=os.environ.get(
        "MDDB_BASE_URL", "http://127.0.0.1:11023/v1"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report; do not publish")
    args = ap.parse_args()

    units = args.units or os.environ.get(
        "ADA_TOOL_USAGE_UNITS", "ada-ha-tony ada-ha-michael ada-dev").split()
    declared = live_declared(args.tools_url, args.api_key)
    if declared is None:
        declared = static_declared()
    now = datetime.now(timezone.utc).astimezone()

    if args.journal_file:
        messages = Path(args.journal_file).read_text().splitlines()
    else:
        messages = journal_lines(units, args.since)

    stats = scan(messages, declared, args.days)
    md = render(stats, declared, units, now)
    if args.dry_run:
        print(md)
        return 0
    if not publish(args.mddb, md, stats, now):
        return 1
    print(f"published {PAGE_KEY}: {len(stats['calls'])} tools called, "
          f"{len(stats['zero_use'])} zero-use, "
          f"{len(stats['unknown'])} undeclared names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
