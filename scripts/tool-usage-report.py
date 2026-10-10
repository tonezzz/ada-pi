#!/usr/bin/env python3
"""Tool-usage census — scan the service journal + session reports for
tool calls and keep the CMS page `report/tool-usage` live. Runs from
ada-tool-usage.timer (daily) on idc03; safe to run by hand anywhere the
journal / $ADA_TRANSCRIPT_DIR is readable.

Two passes over the same traffic:

  journal   — tool_runner's `tool <name> args=…` log lines plus
              consolidation-alias hits (`tool alias <old> -> <new>`)
              and gate denials (`denied <name>: …`). Direct counts, but
              bounded by journal retention (~7d).
  reports   — the transcript pass (card ada-dead-surface-metric):
              session-report JSONs in $ADA_DATA_DIR/reports carry
              session_events kind=tool_call with the AS-CALLED name;
              each is canonicalized through _ALIASES and bucketed into
              trailing 7d/30d windows. Files persist past journal
              retention, so this is the dead-surface source: declared
              tools with zero canonical calls in 30d are flagged as
              merge/exclusion candidates — review only, never
              auto-delete (rare-but-critical tools like lost-mode
              legitimately sit idle).

Usage:
  tool-usage-report.py [--since "-7d"] [--unit ada-ha-tony ...]
                       [--tools-url http://127.0.0.1:8002/api/tools]
                       [--journal-file PATH] [--reports-dir PATH]
                       [--no-reports] [--dry-run]

Env: MDDB_BASE_URL (default http://127.0.0.1:11023/v1),
     ADA_API_KEY (for /api/tools), ADA_TOOL_USAGE_UNITS
     (default "ada-ha-tony ada-ha-michael ada-dev"),
     ADA_TRANSCRIPT_DIR (reports dir resolves to <parent>/reports).
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from backend.report_meta import validate_report_meta  # noqa: E402

CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
PAGE_KEY = "report/tool-usage"
# Session reports live next to the raw transcripts — same convention as
# scripts/report-rollup.py.
ADA_DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent
REPORT_WINDOWS = (7, 30)     # trailing-day buckets for the transcript pass
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


# -- transcript pass (session reports) --------------------------------------

def canonical_name(name: str, aliases: dict[str, str],
                   declared: set[str]) -> str:
    """As-called tool name -> canonical surface name.

    session_events record call.name BEFORE the runner resolves it, so
    the same transforms apply here in dispatch order: soft alias table
    first, then the bare-underscore normalization execute() falls back
    to (`tool name normalized x -> y`). Unresolvable names pass through
    and land in the report's undeclared bucket.
    """
    if name in aliases:
        return aliases[name]
    if name in declared:
        return name
    if "_" in name:
        bare = name.replace("_", "")
        for d in declared:
            if d.replace("_", "") == bare:
                return d
    return name


def _file_day(stem: str) -> str | None:
    """'YYYY-MM-DD-<session>' filename prefix -> 'YYYY-MM-DD'."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-", stem)
    return m.group(1) if m else None


def _event_day(ev: dict, fallback: str | None) -> str | None:
    """Event day as 'YYYY-MM-DD' — the event's own ts when present,
    else the report file's date prefix."""
    ts = str(ev.get("ts") or "")
    if ts:
        try:
            return datetime.fromisoformat(
                ts.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return fallback


def scan_reports(reports_dir: Path, declared: set[str],
                 aliases: dict[str, str], now: datetime,
                 windows: tuple[int, ...] = REPORT_WINDOWS) -> dict:
    """Canonical per-tool call counts from session-report JSONs.

    Each reports/<day>-<session>.json carries session_events rows —
    kind=tool_call for executed calls (tool=as-called name), kind=
    tool_denied for gate denials (demand the gate blocked — still a
    usage signal). Events are bucketed by their own date into each
    trailing window; only files whose events fall inside the widest
    window count toward `files`.
    """
    cutoffs = {w: (now - timedelta(days=w)).date().isoformat()
               for w in windows}
    widest = cutoffs[max(windows)]
    calls = {w: collections.Counter() for w in windows}
    denied: dict[str, int] = collections.Counter()
    as_called: dict[str, dict] = collections.defaultdict(
        collections.Counter)
    last_seen: dict[str, str] = {}
    files = 0
    for path in sorted(Path(reports_dir).glob("*.json")):
        file_day = _file_day(path.stem)
        # Cheap pre-filter: a date-prefixed file entirely outside the
        # widest window can't contribute — skip the JSON parse.
        if file_day and file_day < widest:
            continue
        try:
            rpt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        counted_file = False
        for ev in rpt.get("session_events") or []:
            kind = ev.get("kind")
            if kind not in ("tool_call", "tool_denied"):
                continue
            day = _event_day(ev, file_day)
            if day is None or day < widest:
                continue
            counted_file = True
            name = str(ev.get("tool") or "")
            canon = canonical_name(name, aliases, declared)
            if kind == "tool_denied":
                denied[canon] += 1
                continue
            as_called[canon][name] += 1
            for w in windows:
                if day >= cutoffs[w]:
                    calls[w][canon] += 1
            if day > last_seen.get(canon, ""):
                last_seen[canon] = day
        if counted_file:
            files += 1
    c30 = calls[max(windows)]
    return {
        "files": files,
        "windows": windows,
        "calls": calls,
        "denied": denied,
        "as_called": {k: dict(v) for k, v in as_called.items()},
        "last_seen": last_seen,
        "unknown": {t: n for t, n in c30.items() if t not in declared},
        "zero_call": sorted(t for t in declared if c30.get(t, 0) == 0),
    }


def _render_reports(rstats: dict, declared: set[str]) -> list[str]:
    w7, w30 = rstats["windows"][0], rstats["windows"][-1]
    c7, c30 = rstats["calls"][w7], rstats["calls"][w30]
    lines = [
        "",
        f"## Canonical calls — transcript pass "
        f"({rstats['files']} session reports)",
        "",
        "As-called names resolved through _ALIASES before counting — "
        "this is the dead-surface view (files outlive the journal).",
        "",
        f"| tool | calls {w7}d | calls {w30}d | denied {w30}d "
        f"| last called | note |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(declared | set(c30),
                       key=lambda t: (-c30.get(t, 0), t)):
        notes = []
        if name not in declared:
            notes.append("**not declared**")
        elif c30.get(name, 0) == 0:
            notes.append("zero-call candidate")
        variants = {k: v for k, v
                    in (rstats["as_called"].get(name) or {}).items()
                    if k != name}
        if variants:
            notes.append("via alias " + ", ".join(
                f"{k}×{v}" for k, v in sorted(variants.items())))
        lines.append(
            f"| {name} | {c7.get(name, 0)} | {c30.get(name, 0)} "
            f"| {rstats['denied'].get(name, 0)} "
            f"| {rstats['last_seen'].get(name, '—')} "
            f"| {'; '.join(notes)} |")
    zero = rstats["zero_call"]
    lines += [
        "",
        f"## Zero-call tools — {w30}d (merge/exclusion candidates)",
        "",
        f"**{len(zero)} of {len(declared)} declared tools saw no "
        f"canonical calls in {w30}d.** Review list for the merge "
        "program — NOT auto-delete: rare-but-critical tools "
        "(lost-mode, emergency paths) legitimately sit idle. A zero "
        "count starts a review, nothing more.",
        "",
    ]
    lines += [f"- `{t}`" for t in zero] or ["- none"]
    return lines


def render(stats: dict, declared: set[str], units: list[str],
           now: datetime, rstats: dict | None = None) -> str:
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
    if rstats is not None:
        lines += _render_reports(rstats, declared)
        if rstats["unknown"]:
            lines += ["", "## Calls to undeclared names — transcripts",
                      ""]
            for t, n in sorted(rstats["unknown"].items(),
                               key=lambda kv: -kv[1]):
                lines.append(f"- `{t}` — {n} call(s) in 30d")
    lines += ["", f"---", f"journal units: {', '.join(units)} — "
              "refreshed by ada-tool-usage.timer"]
    return "\n".join(lines)


def publish(mddb: str, content_md: str, stats: dict, now: datetime,
            rstats: dict | None = None) -> bool:
    zero30 = f", {len(rstats['zero_call'])} zero-call-30d" \
        if rstats is not None else ""
    meta = {
        "kind": ["report"], "domain": ["tools"], "suite": ["tool-usage"],
        "slug": [PAGE_KEY],
        "title": [f"Tool usage — {now:%Y-%m-%d}"],
        "format": ["markdown"], "instance": ["idc01"],
        "updated": [now.isoformat(timespec="seconds")],
        "fresh_for": ["25h"],
        # journal-line census — direct counts, but only covers the units
        # scanned, so not 'high'
        "confidence": ["medium"],
        "summary": [f"{len(stats['calls'])} tools called, "
                    f"{len(stats['zero_use'])} zero-use, "
                    f"{len(stats['unknown'])} undeclared{zero30}"],
        "timeline": [f"{now.isoformat(timespec='minutes')}: census "
                     f"{len(stats['calls'])}/{len(stats['calls']) + len(stats['zero_use'])} tools active"],
    }
    # Report meta contract (ssot.apps.ada-cms-reports.yml) — refuse to
    # publish a page missing required fields; warnings log but don't block.
    check = validate_report_meta(meta)
    if not check["ok"]:
        print(f"meta contract violation, not publishing: {check['missing']}",
              file=sys.stderr)
        return False
    for w in check["warnings"]:
        print(f"meta warning: {w}", file=sys.stderr)
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
    ap.add_argument("--reports-dir", type=Path, default=ADA_DATA_DIR / "reports",
                    help="session-report JSON dir for the transcript pass "
                         "(default <ADA_TRANSCRIPT_DIR>/../reports)")
    ap.add_argument("--no-reports", action="store_true",
                    help="skip the transcript pass (journal census only)")
    ap.add_argument("--mddb", default=os.environ.get(
        "MDDB_BASE_URL", "http://127.0.0.1:11023/v1"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report; do not publish")
    args = ap.parse_args()

    units = args.units or os.environ.get(
        "ADA_TOOL_USAGE_UNITS", "ada-ha-tony ada-ha-michael ada-dev").split()
    declared = live_declared(args.tools_url, args.api_key)
    aliases: dict[str, str] = {}
    if declared is None:
        declared = static_declared()
    try:
        aliases = _load_lint().collect_surface(REPO)["aliases"]
    except Exception as exc:
        print(f"alias table unreadable ({exc}) — transcript pass counts "
              "as-called names", file=sys.stderr)
    now = datetime.now(timezone.utc).astimezone()

    if args.journal_file:
        messages = Path(args.journal_file).read_text().splitlines()
    else:
        messages = journal_lines(units, args.since)

    stats = scan(messages, declared, args.days)
    rstats = None
    if not args.no_reports:
        if args.reports_dir.is_dir():
            rstats = scan_reports(args.reports_dir, declared, aliases, now)
        else:
            print(f"no reports dir {args.reports_dir} — transcript pass "
                  "skipped", file=sys.stderr)
    md = render(stats, declared, units, now, rstats)
    if args.dry_run:
        print(md)
        return 0
    if not publish(args.mddb, md, stats, now, rstats):
        return 1
    print(f"published {PAGE_KEY}: {len(stats['calls'])} tools called, "
          f"{len(stats['zero_use'])} zero-use, "
          f"{len(stats['unknown'])} undeclared names"
          + (f", {len(rstats['zero_call'])} zero-call-30d"
             if rstats is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
