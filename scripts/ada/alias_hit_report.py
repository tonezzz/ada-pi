#!/usr/bin/env python3
"""Alias-hit rollup — emitted-name census over the session transcript
stores, feeding the deprecation path for the _ALIASES compat layer
(card ada-alias-telemetry).

Counts per EMITTED tool name and splits them declared vs alias vs
unknown. Per alias row it tracks hits, last-hit day, and quiet days —
an alias at 0 hits for --eligible-days (default 14) is
removal-eligible: retire it by deleting its _ALIASES row AND its entry
in the family's `absorbed` list (tool-lint enforces both sides, so the
two edits must land in one commit). The same pass emits the
dead-surface list (declared tools nobody emitted) for card
ada-dead-surface-metric.

Sources:
  call-logs  $ADA_CALL_LOG_DIR or <ADA_TRANSCRIPT_DIR>/../call-logs —
             <day>-<session>.jsonl, `function_call` events. `name` is
             the AS-EMITTED name (written before alias resolution), so
             stale names are visible here even though the session
             report's tool_call.tool carries the resolved canonical.
  reports    <data>/reports/<day>-<session>.json — used only for
             sessions with no call-log file at all (journald-era or
             ADA_CALL_LOG=0 sessions); emitted= exists on events logged
             after 2026-10-10, older ones fall back to canonical `tool`
             and can only under-count alias hits.
  journal    `tool <name> args=` / `tool alias <old> -> <new>` lines —
             the ws/HTTP path (POST /api/tools) resolves aliases inside
             execute() without a call-log, so journal hits are kept as
             a separate column and counted into eligibility.

Publishes CMS page `report/alias-hits` (kind=report → reports-index)
and writes a machine-readable rollup to <data>/runs/alias-hits/<date>.json.

Usage:
  alias_hit_report.py [--days 14] [--eligible-days 14] [--dry-run]
                      [--call-log-dir PATH] [--reports-dir PATH]
                      [--no-journal] [--unit UNIT ...]
                      [--out PATH] [--mddb URL]

Env: ADA_CALL_LOG_DIR, ADA_TRANSCRIPT_DIR, ADA_TOOL_USAGE_UNITS,
     MDDB_BASE_URL, ADA_CMS_COLLECTION.
Exit: 0 ok / 1 publish failed / 2 setup error.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
from backend.report_meta import validate_report_meta  # noqa: E402

DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent
CALL_LOG_DIR = Path(os.environ.get(
    "ADA_CALL_LOG_DIR", str(DATA_DIR / "call-logs")))
CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
PAGE_KEY = "report/alias-hits"
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
RE_CALL = re.compile(r"^tool ([a-z0-9_]+) args=")
RE_ALIAS = re.compile(r"^tool alias ([a-z0-9_]+) -> ([a-z0-9_]+)")
ELIGIBLE_DEFAULT = 14


def _load_script(name: str):
    """Import a sibling script by path (scripts/ isn't a package)."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), REPO / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def surface() -> tuple[set[str], dict[str, str], dict[str, str]]:
    """(declared, alias->canonical, alias->family) from the static census
    + ssot.tool-surface.yml — the contract, not the live process."""
    lint = _load_script("tool-lint.py")
    surf = lint.collect_surface(REPO)
    import yaml
    ssot = yaml.safe_load(
        (REPO / "docs/ssot/ssot.tool-surface.yml").read_text(
            encoding="utf-8")) or {}
    fam_of: dict[str, str] = {}
    for fam, cfg in (ssot.get("families") or {}).items():
        for n in (cfg or {}).get("absorbed") or []:
            fam_of[str(n)] = str(fam)
    return surf["declared"], surf["aliases"], fam_of


def _file_day(p: Path) -> str | None:
    m = DAY_RE.match(p.name)
    return m.group(1) if m else None


def iter_call_log_calls(call_log_dir: Path, day_lo: str, day_hi: str):
    """Yield (day, session, emitted_name) for function_call events."""
    if not call_log_dir.is_dir():
        return
    for p in sorted(call_log_dir.glob("*.jsonl")):
        day = _file_day(p)
        if not day or day < day_lo or day > day_hi:
            continue
        session = p.stem[11:]
        try:
            lines = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in lines.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "function_call" and ev.get("name"):
                yield day, str(ev.get("session_id") or session), str(ev["name"])


def logged_sessions(call_log_dir: Path) -> set[str]:
    """Session ids with ANY call-log file — used to keep the reports
    fallback from double-counting sessions the JSONL already covers."""
    if not call_log_dir.is_dir():
        return set()
    out = set()
    for p in call_log_dir.glob("*.jsonl"):
        if _file_day(p):
            out.add(p.stem[11:])
    return out


def iter_report_calls(reports_dir: Path, day_lo: str, day_hi: str,
                      skip_sessions: set[str]):
    """Yield (day, session, emitted|canonical) from session-report
    tool_call events, for sessions the call-logs don't cover."""
    if not reports_dir.is_dir():
        return
    for p in sorted(reports_dir.glob("*.json")):
        day = _file_day(p)
        if not day or day < day_lo or day > day_hi:
            continue
        session = p.stem[11:]
        if session in skip_sessions:
            continue
        try:
            rpt = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for ev in rpt.get("session_events") or []:
            if ev.get("kind") != "tool_call":
                continue
            name = ev.get("emitted") or ev.get("tool")
            if name:
                yield day, session, str(name)


def journal_scan(units: list[str], since: str) -> dict:
    """Alias hits + dispatched canonical names from the service journal —
    covers the ws/HTTP execute() path, which writes no call-log.
    Returns {"alias_days": {alias: last-day}, "calls": set(), ...} or
    None when no unit produced lines."""
    out: dict = {"alias_days": {}, "calls": set(), "alias_hits": {}}
    got_any = False
    base = ["journalctl", "-o", "json", "--no-pager",
            "--output-fields", "MESSAGE,__REALTIME_TIMESTAMP",
            "--since", since]
    for scope in (["--user"], []):
        for unit in units:
            try:
                raw = subprocess.check_output(
                    base[:1] + scope + base[1:] + ["-u", unit],
                    text=True, timeout=60, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError,
                    subprocess.TimeoutExpired, OSError):
                continue
            for line in raw.splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("MESSAGE")
                if not isinstance(msg, str):
                    continue
                day = ""
                ts = rec.get("__REALTIME_TIMESTAMP")
                try:
                    day = datetime.fromtimestamp(
                        int(ts) / 1e6, timezone.utc).date().isoformat()
                except (TypeError, ValueError, OSError):
                    pass
                m = RE_ALIAS.match(msg)
                if m:
                    got_any = True
                    old = m.group(1)
                    out["alias_hits"][old] = out["alias_hits"].get(old, 0) + 1
                    out["calls"].add(m.group(2))
                    if day > out["alias_days"].get(old, ""):
                        out["alias_days"][old] = day
                    continue
                m = RE_CALL.match(msg)
                if m:
                    got_any = True
                    out["calls"].add(m.group(1))
        if got_any:
            break   # --user view had data; don't double-count system view
    return out if got_any else None


def rollup(declared: set[str], aliases: dict[str, str], fam_of: dict[str, str],
           calls: list[tuple[str, str, str]], journal: dict | None,
           *, today: date, days: int, eligible_days: int,
           sources: list[str]) -> dict:
    """Build the rollup dict. calls = [(day, session, emitted_name)]."""
    day_lo = (today - timedelta(days=days - 1)).isoformat()
    hits: dict[str, int] = collections.Counter()
    last_day: dict[str, str] = {}
    sessions: set[str] = set()
    data_from = ""
    for day, sess, name in calls:
        hits[name] += 1
        sessions.add(sess)
        if day > last_day.get(name, ""):
            last_day[name] = day
        if not data_from or day < data_from:
            data_from = day
    if not data_from:
        data_from = today.isoformat()

    def kind_of(name: str) -> str:
        if name in declared:
            return "declared"
        if name in aliases:
            return "alias"
        return "unknown"

    names = {
        n: {"kind": kind_of(n), "hits": hits[n],
            "canonical": aliases.get(n), "last_day": last_day.get(n)}
        for n in hits}

    j_days = (journal or {}).get("alias_days") or {}
    j_hits = (journal or {}).get("alias_hits") or {}
    # Observed silence: never-hit aliases are only as quiet as the data
    # is deep — quiet = days since the OLDEST evidence we have.
    rows = {}
    for old, canon in sorted(aliases.items()):
        last = max(last_day.get(old, ""), j_days.get(old, ""))
        quiet = ((today - date.fromisoformat(last)).days if last
                 else (today - date.fromisoformat(data_from)).days)
        rows[old] = {
            "canonical": canon, "family": fam_of.get(old, ""),
            "transcript_hits": hits.get(old, 0),
            "journal_hits": j_hits.get(old, 0),
            "last_hit": last or None, "quiet_days": quiet,
            "removal_eligible": quiet >= eligible_days,
        }

    # Dead surface — declared tools with no call on any path. An emitted
    # alias means its canonical WAS called, so alias targets count too.
    used = {n for n in hits if n in declared}
    used |= {aliases[n] for n in hits if n in aliases}
    if journal:
        used |= {n for n in journal["calls"] if n in declared}
    dead = sorted(declared - used)

    return {
        "ref": f"run:alias-hits/{today.isoformat()}",
        "kind": "alias-rollup",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"days": days, "from": day_lo,
                   "to": today.isoformat()},
        "coverage": {"data_from": data_from,
                     "sessions": len(sessions),
                     "window_complete": data_from <= (
                         today - timedelta(days=eligible_days)
                     ).isoformat(),
                     "sources": sources},
        "names": names,
        "aliases": rows,
        "unknown": {n: names[n]["hits"] for n in names
                    if names[n]["kind"] == "unknown"},
        "dead_surface": dead,
        "totals": {
            "emitted_names": len(names), "calls": sum(hits.values()),
            "alias_rows": len(rows),
            "alias_hit_names": sum(1 for r in rows.values()
                                   if r["transcript_hits"]
                                   or r["journal_hits"]),
            "removal_eligible": sum(1 for r in rows.values()
                                    if r["removal_eligible"]),
            "unknown_names": len([n for n in names
                                  if names[n]["kind"] == "unknown"]),
            "dead_surface": len(dead),
        },
    }


def render(r: dict, now: datetime) -> str:
    days = r["window"]["days"]
    cov = r["coverage"]
    t = r["totals"]
    lines = [
        f"# Alias hits — {now:%Y-%m-%d %H:%M} (last {days}d)", "",
        f"{t['emitted_names']} names emitted · {t['calls']} calls · "
        f"{t['alias_rows']} compat rows tracked · "
        f"**{t['removal_eligible']} removal-eligible** · "
        f"{t['unknown_names']} unknown names · "
        f"{t['dead_surface']} dead-surface tools", "",
        (f"Coverage: transcript data from {cov['data_from']} "
         f"({'full window' if cov['window_complete'] else 'PARTIAL — quiet-day counts only span the observed data'}); "
         f"{cov['sessions']} sessions; sources: {', '.join(cov['sources'])}."),
        "",
        "## Compat layer (`_ALIASES` rows)", "",
        "| alias | → canonical | hits | ws/journal | last hit | quiet | status |",
        "|---|---|---|---|---|---|---|",
    ]
    for old, row in sorted(r["aliases"].items(),
                           key=lambda kv: (-(kv[1]["transcript_hits"]
                                             + kv[1]["journal_hits"]),
                                           kv[0])):
        status = ("**removal-eligible**" if row["removal_eligible"]
                  else "warm" if (row["transcript_hits"]
                                  or row["journal_hits"]) else "quiet")
        lines.append(
            f"| `{old}` | `{row['canonical']}` | {row['transcript_hits']} "
            f"| {row['journal_hits']} | {row['last_hit'] or '—'} "
            f"| {row['quiet_days']}d | {status} |")

    eligible = [a for a, row in r["aliases"].items()
                if row["removal_eligible"]]
    lines += ["", "## Removal path", "",
              ("Eligible now: " + ", ".join(f"`{a}`" for a in eligible)
               if eligible else
               "Nothing eligible yet — removal needs 0 hits for "
               f"{ELIGIBLE_DEFAULT}d of observed data."),
              "",
              "To retire an alias: delete its `_ALIASES` row AND its entry "
              "in the family's `absorbed` list in "
              "docs/ssot/ssot.tool-surface.yml — tool-lint fails on a "
              "half-removal in either direction (absorbed-without-alias "
              "and stray-alias are both errors). Gates resolve via lint."]

    if r["unknown"]:
        lines += ["", "## Unknown emitted names (not declared, not aliased)",
                  ""]
        for n, h in sorted(r["unknown"].items(), key=lambda kv: -kv[1]):
            lines.append(
                f"- `{n}` — {h} call(s), last {r['names'][n]['last_day']} "
                "(stale habit or typo drift — no seat to absorb into)")
    lines += ["", "## Dead surface (declared, 0 calls in window)", ""]
    if r["dead_surface"]:
        lines.append(", ".join(f"`{n}`" for n in r["dead_surface"]))
    else:
        lines.append("none — every declared tool saw a call")
    lines += ["", "---",
              "emitted names from call-logs function_call events "
              "(journald-immune); ws/HTTP path counted from the service "
              "journal. Feeds ada-dead-surface-metric. Refreshed by "
              "ada-alias-report.timer"]
    return "\n".join(lines)


def publish(mddb: str, content_md: str, r: dict, now: datetime) -> bool:
    t = r["totals"]
    meta = {
        "kind": ["report"], "domain": ["tools"], "suite": ["alias-hits"],
        "slug": [PAGE_KEY],
        "title": [f"Alias hits — {now:%Y-%m-%d}"],
        "format": ["markdown"], "instance": ["idc03"],
        "updated": [now.isoformat(timespec="seconds")],
        "fresh_for": ["8d"],
        # file-based census — immune to journald suppression, but only
        # covers paths that write call-logs (+ journal cross-check)
        "confidence": ["medium"],
        "summary": [f"{t['alias_hit_names']}/{t['alias_rows']} aliases hit, "
                    f"{t['removal_eligible']} removal-eligible, "
                    f"{t['dead_surface']} dead-surface, "
                    f"{t['unknown_names']} unknown names"],
        "timeline": [f"{now.isoformat(timespec='minutes')}: rollup "
                     f"{t['calls']} calls over {r['window']['days']}d, "
                     f"{t['removal_eligible']} aliases eligible"],
    }
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
    ap.add_argument("--days", type=int, default=14,
                    help="scan window in days (default 14 = the "
                         "eligibility window)")
    ap.add_argument("--eligible-days", type=int, default=ELIGIBLE_DEFAULT,
                    help="quiet days before an alias is removal-eligible")
    ap.add_argument("--call-log-dir", type=Path, default=CALL_LOG_DIR)
    ap.add_argument("--reports-dir", type=Path, default=DATA_DIR / "reports")
    ap.add_argument("--no-journal", action="store_true",
                    help="skip the journalctl cross-check (offline hosts)")
    ap.add_argument("--unit", action="append", dest="units")
    ap.add_argument("--since", default=None,
                    help="journalctl --since (default: -<days>d)")
    ap.add_argument("--out", type=Path, default=None,
                    help="rollup JSON path (default <data>/runs/"
                         "alias-hits/<today>.json; '-' = stdout)")
    ap.add_argument("--mddb", default=os.environ.get(
        "MDDB_BASE_URL", "http://127.0.0.1:11023/v1"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the page + JSON; do not publish")
    args = ap.parse_args()

    try:
        declared, aliases, fam_of = surface()
    except Exception as exc:
        print(f"surface census failed: {exc}", file=sys.stderr)
        return 2
    today = datetime.now(timezone.utc).date()
    day_lo = (today - timedelta(days=args.days - 1)).isoformat()
    day_hi = today.isoformat()

    calls = list(iter_call_log_calls(args.call_log_dir, day_lo, day_hi))
    sources = ["call-logs"] if args.call_log_dir.is_dir() else []
    covered = {s for _, s, _ in calls}
    # Reports fallback covers only sessions with no call-log file at all.
    rep = list(iter_report_calls(
        args.reports_dir, day_lo, day_hi,
        logged_sessions(args.call_log_dir) | covered))
    if rep:
        calls += rep
        sources.append("reports")

    journal = None
    if not args.no_journal:
        units = args.units or os.environ.get(
            "ADA_TOOL_USAGE_UNITS",
            "ada-ha-tony ada-ha-michael ada-dev").split()
        journal = journal_scan(units, args.since or f"-{args.days}d")
        if journal:
            sources.append("journal")

    if not calls and journal is None and not args.dry_run:
        # No transcript data and no journal — publishing would stamp a
        # '0 calls' page that looks like total quiescence. Refuse.
        print("nothing to report: no call-log files, no report events, "
              "no journal lines", file=sys.stderr)
        return 2

    r = rollup(declared, aliases, fam_of, calls, journal,
               today=today, days=args.days,
               eligible_days=args.eligible_days, sources=sources or ["none"])
    now = datetime.now(timezone.utc).astimezone()
    md = render(r, now)
    if args.dry_run:
        print(md)
        print("\n```json\n" + json.dumps(r, ensure_ascii=False, indent=2)
              + "\n```")
        return 0

    if args.out == Path("-"):
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        out = args.out or (args.call_log_dir.parent / "runs" / "alias-hits"
                           / f"{today.isoformat()}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"rollup -> {out}")
    if not publish(args.mddb, md, r, now):
        return 1
    print(f"published {PAGE_KEY}: {r['totals']['alias_hit_names']} aliases "
          f"hit, {r['totals']['removal_eligible']} removal-eligible, "
          f"{r['totals']['dead_surface']} dead-surface")
    return 0


if __name__ == "__main__":
    sys.exit(main())
