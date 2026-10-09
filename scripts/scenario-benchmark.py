#!/usr/bin/env python3
"""Ada benchmark runner — run a named suite from tests/benchmark.yml and
write ONE aggregate 'kind: benchmark' doc to ada-ha-scenario-reports so
progress is measurable across runs (compare meta.score per suite).

  scenario-benchmark.py --suite research [--url WS] [--api-key K]
                        [--mddb URL] [--keys-file PATH] [--dry-run]
                        [--report-cms]        # also publish a cms page

Scoring: pass=1, flaky=0.5, fail/skip=0; suite score = mean. Audit policy
(tests/benchmark.yml 'policy') adds violations: required_first_try failing,
or write tools seen in a scenario not listed under write_allowed_in.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from backend.report_meta import validate_report_meta  # noqa: E402

BENCH = os.path.join(REPO, "tests", "benchmark.yml")
SCEN_DIR = os.path.join(REPO, "tests", "scenarios-live")
COLLECTION = "ada-ha-scenario-reports"

import re
# Same taxonomy as scenario-report.py: an environment-level rejection is
# not a model failure. ws handshake rejects (403/400 — auth windows during
# ada restarts, device-key churn) were being scored as real fails with 0
# turns (2026-09-30 casting run scored 0.045 during a parallel session).
UNSCORED_RE = re.compile(
    r"429|quota|rate.?limit|resource.?exhaust|too many requests"
    r"|connect call failed|connection refused|no ready event"
    r"|invalid handshake|connection rejected|HTTP 40[03]|InvalidStatus",
    re.I)


def _load() -> dict:
    return yaml.safe_load(open(BENCH).read()) or {}


def _scenario_tier(path: str) -> str:
    try:
        return (yaml.safe_load(open(path).read()) or {}).get("tier") or "full"
    except Exception:
        return "full"


def _expand(suite: str, bench: dict) -> list[str]:
    spec = (bench.get("suites") or {}).get(suite)
    if spec is None:
        print(f"unknown suite {suite!r}; suites: {sorted(bench.get('suites') or {})}",
              file=sys.stderr)
        sys.exit(2)
    names: list[str] = []
    for item in spec if isinstance(spec, list) else [spec]:
        if item == "@smoke":
            names += [os.path.splitext(os.path.basename(p))[0].replace("_", "-")
                      for p in glob.glob(os.path.join(SCEN_DIR, "*.yaml"))
                      if _scenario_tier(p) == "smoke"]
        elif item == "@all":
            names += [os.path.splitext(os.path.basename(p))[0].replace("_", "-")
                      for p in glob.glob(os.path.join(SCEN_DIR, "*.yaml"))]
        else:
            names.append(item)
    return names


def _slug_file(name: str) -> str:
    """Map scenario name back to its yaml file (files use underscores)."""
    p = os.path.join(SCEN_DIR, name.replace("-", "_") + ".yaml")
    if os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(SCEN_DIR, "*.yaml"))
    for h in hits:
        try:
            if (yaml.safe_load(open(h).read()) or {}).get("name") == name:
                return h
        except Exception:
            pass
    return p


def _keys(path: str) -> dict:
    try:
        return json.loads(open(path).read())
    except (OSError, ValueError):
        return {}


def _key_entry(keys: dict, name: str) -> tuple[str, str | None]:
    """(raw_key, device_id) — file format: {name: '<key>' | {key, device}}."""
    entry = keys.get(name)
    if isinstance(entry, dict):
        return str(entry.get("key") or ""), entry.get("device")
    return str(entry or ""), None


def _env_value(path: str, name: str) -> str:
    """Read NAME=value from a dotenv file (tolerates 'export ' prefix)."""
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _run_one(path: str, url: str, api_key: str, keys: dict) -> tuple[str, int, dict | None, str]:
    """One scenario, retry-once → flaky. Returns (status, runs, events, out).

    Honors per-scenario `env_file:` and `key_name:` like scenario-report.py —
    without this, key-scoped scenarios (doc_policy_gate etc.) silently run
    under the admin key and fail for the wrong reason."""
    driver = os.path.join(HERE, "scenario-live.py")
    try:
        spec = yaml.safe_load(open(path).read()) or {}
    except Exception:
        spec = {}
    if spec.get("env_file"):
        api_key = _env_value(str(spec["env_file"]), "ADA_API_KEY") or api_key
    if spec.get("key_name"):
        api_key, device = _key_entry(keys, spec["key_name"])
        if not api_key:
            return "skip", 0, None, f"key {spec['key_name']} not in keys file"
        if device:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}device_id={device}"
    for attempt in (1, 2):
        ev_path = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                               f"bench-{os.path.basename(path)}-{attempt}.json")
        # Remove any stale events file first — if the child dies before
        # writing (e.g. 403 from a missing ADA_API_KEY), a leftover from a
        # previous suite would be read and report yesterday's durations
        # (2026-10-01: 02:00 run replayed the 17:08 results exactly).
        try:
            os.unlink(ev_path)
        except OSError:
            pass
        cmd = [sys.executable, driver, path, "--url", url,
               "--events-json", ev_path]
        if api_key:
            cmd += ["--api-key", api_key]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=1800)
        except subprocess.TimeoutExpired:
            return "fail", attempt, None, "timeout"
        out = (proc.stdout or "") + (proc.stderr or "")
        try:
            events = json.loads(open(ev_path).read())
        except (OSError, ValueError):
            events = None
        if proc.returncode == 0:
            return ("flaky" if attempt == 2 else "pass"), attempt, events, out
        if proc.returncode == 3:          # preflight gate
            return "infra", attempt, events, out
        if proc.returncode == 4:          # needs_tools absent
            return "unimplemented", attempt, events, out
        # rc=1 but the output shows an environment failure (ws handshake
        # rejected, upstream throttling) — classify by cause, not code:
        # a 0s "fail" with no turns is harness noise, not a model result.
        if UNSCORED_RE.search(out) and not (
                events or {}).get("turns"):
            return ("quota" if re.search(
                r"429|quota|rate.?limit|resource.?exhaust", out, re.I)
                else "infra"), attempt, events, out
    return "fail", 2, events, out


def _post(url: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as exc:
        print(f"  mddb write failed: {exc}", file=sys.stderr)
        return False


def _fetch_prior_statuses(mddb: str, suite: str, before: str) -> dict[str, str]:
    """scenario -> status from the most recent prior benchmark doc for this
    suite (key < the one we're about to write). Used for regression diffs."""
    try:
        req = urllib.request.Request(
            f"{mddb.rstrip('/')}/search",
            data=json.dumps({"collection": COLLECTION, "query": "",
                             "limit": 300}).encode(),
            headers={"Content-Type": "application/json"})
        docs = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception:
        return {}
    cands = [d for d in docs
             if d.get("key", "").startswith(f"benchmark/{suite}-")
             and d.get("key", "") < f"benchmark/{suite}-{before}"]
    if not cands:
        return {}
    latest = sorted(d["key"] for d in cands)[-1]
    meta = (next(d for d in cands if d["key"] == latest).get("meta") or {})
    out: dict[str, str] = {}
    for s in meta.get("scenarios") or []:
        name, _, status = str(s).rpartition(":")
        if name:
            out[name] = status
    return out


def _report_worthy(rows: list, violations: list[str],
                   prior: dict[str, str]) -> list[str]:
    """Deterministic report-worthiness judgment — no LLM. A report is
    worthy when: a scenario fails, a policy violation fires, a scenario
    regressed vs the previous run of the same suite, or score data shows
    persistent flakiness. Everything else is noise."""
    items: list[str] = []
    for name, status, *_ in rows:
        if status == "fail":
            was = prior.get(name)
            items.append(f"{name}: FAIL" + (f" (regression — was {was})"
                                           if was and was != "fail" else ""))
        elif status == "flaky" and prior.get(name) == "flaky":
            items.append(f"{name}: persistently flaky")
    items.extend(f"policy: {v}" for v in violations)
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True)
    ap.add_argument("--url", default=os.environ.get("ADA_LIVE_URL")
                    or "ws://127.0.0.1:8002/ws")
    ap.add_argument("--api-key", default=os.environ.get("ADA_API_KEY") or "")
    # Benchmark summary docs also live in ada-ha-scenario-reports (ops
    # collection) — prefer the ops store; the leader's /add embeds inline
    # and times out under embedding rate limits (see scenario-report.py).
    ap.add_argument("--mddb", default=os.environ.get("MDDB_OPS_URL")
                    or os.environ.get("MDDB_BASE_URL")
                    or "http://127.0.0.1:11023/v1")
    ap.add_argument("--keys-file", default=os.environ.get("ADA_KEYS_FILE")
                    or "")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-cms", action="store_true",
                    help="also publish a markdown page to ada-cms-pages")
    ap.add_argument("--max-scenarios", type=int, default=0,
                    help="cap the run at N scenarios — the doc is marked "
                         "partial/invalid so a probe run can't pollute the "
                         "suite baseline")
    ap.add_argument("--budget-s", type=float,
                    default=float(os.environ.get("BENCH_BUDGET_S") or 0),
                    help="stop starting new scenarios after N seconds and "
                         "write a partial report — keep under the unit's "
                         "TimeoutStartSec so a slow run never dies silent")
    args = ap.parse_args()

    # One benchmark at a time per host — concurrent runs share the same
    # Ada session and the same vcast screens, so they contaminate each
    # other (2026-09-30: casting suite scored 0.0 while the hourly smoke
    # tier held the session; every scenario failed with 0 tool calls).
    # Lock is a TCP bind — the smoke tier runs in a host-network container
    # whose /tmp is private, so a file lock can't reach it; a port can.
    import socket
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 8199))
    except OSError:
        print("another scenario benchmark is already running — refusing "
              "(concurrent suites share the Ada session and corrupt each "
              "other's results)", flush=True)
        return 2
    _lock.listen(1)

    bench = _load()
    keys = _keys(args.keys_file) if args.keys_file else {}
    policy = bench.get("policy") or {}
    required = set(policy.get("required_first_try") or [])
    write_tools = set(policy.get("write_tools") or [])
    write_allowed = set(policy.get("write_allowed_in") or [])

    names = _expand(args.suite, bench)
    if args.max_scenarios:
        names = names[: args.max_scenarios]

    rows, violations = [], []
    run_metrics: dict[str, str] = {}
    run_t0 = time.time()

    def _progress(note: str) -> None:
        """Live progress doc — a run killed mid-suite (TimeoutStartSec,
        SIGKILL) still leaves every completed scenario on record instead
        of nothing (2026-10-08/09: casting ran 2h, was killed, and wrote
        no report — the suite looked hung while it was just slow)."""
        if args.dry_run:
            return
        table = "\n".join(
            f"| {n} | {s} | {t} | {c} | {d:.0f}s |"
            for n, s, t, c, d in rows)
        _post(f"{args.mddb.rstrip('/')}/add", {
            "collection": COLLECTION,
            "key": f"benchmark/{args.suite}/live",
            "lang": "en",
            "contentMd": (f"# Benchmark `{args.suite}` — {note}\n\n"
                          f"{len(rows)}/{len(names)} scenarios completed.\n\n"
                          "| scenario | status | turns | tools | dur |\n"
                          "|---|---|---|---|---|\n" + table + "\n"),
            "meta": {"kind": ["benchmark-progress"], "suite": [args.suite],
                     "partial": ["true"], "note": [note],
                     "ts": [datetime.datetime.now().isoformat(
                         timespec="seconds")],
                     "scenarios": [f"{n}:{s}" for n, s, *_ in rows]}})

    def _on_sigterm(signo, _frame):
        # TimeoutStartSec lands as SIGTERM — leave the trail, then die.
        try:
            _progress(f"KILLED by signal {signo}")
        except Exception:
            pass
        os._exit(128 + signo)

    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)

    _progress("run started")
    for name in names:
        if args.budget_s and time.time() - run_t0 > args.budget_s:
            print(f"== budget {args.budget_s:.0f}s exhausted — "
                  f"{len(names) - len(rows)} scenario(s) unrun",
                  flush=True)
            break
        path = _slug_file(name)
        if not os.path.exists(path):
            print(f"== {name}: SKIP (no yaml)", flush=True)
            rows.append((name, "skip", 0, 0, 0.0))
            _progress("in progress")
            continue
        status, runs, events, out = _run_one(path, args.url, args.api_key,
                                           keys)
        turns = (events or {}).get("turns") or []
        n_tools = sum(len(t.get("tools") or []) for t in turns)
        dur = float((events or {}).get("duration_s") or 0)
        rows.append((name, status, len(turns), n_tools, dur))
        # http_check `capture:` values (e.g. dub-metrics sidecars) —
        # trended on the benchmark doc, so the suite tracks real numbers
        # (cue_overflow_pct, voices_per_speaker) not just pass/fail
        for t in turns:
            for mk, mv in (t.get("metrics") or {}).items():
                run_metrics[f"{name}.{mk}"] = str(mv)
        print(f"== {name}: {status} ({runs} runs, {n_tools} tool calls, "
              f"{dur:.0f}s)", flush=True)
        _progress("in progress")
        if name in required and status != "pass":
            violations.append(f"{name}: required_first_try failed ({status})")
        if name not in write_allowed:
            used = {t for turn in turns for t in (turn.get("tools") or [])}
            bad = used & write_tools
            if bad:
                violations.append(f"{name}: write tools outside policy {sorted(bad)}")

    score_map = {"pass": 1.0, "flaky": 0.5}
    # environment/feature-absence results are excluded from the mean —
    # an mddb outage or an unshipped tool must not drag the model score
    unscored = {"infra", "unimplemented", "quota", "skip-quota"}
    scored = [score_map.get(s, 0.0) for _, s, *_ in rows
              if s not in unscored]
    score = round(sum(scored) / len(scored), 3) if scored else 0.0
    now = datetime.datetime.now()

    # Run validity — a run where the majority of scenarios came back
    # infra/quota measured the environment, not Ada (e.g. Ada restarting
    # mid-run, upstream quota exhausted). Stamped on the benchmark doc so
    # trend queries and scenario-prune.py can exclude it from the baseline.
    unscored_run = sum(1 for _, s, *_ in rows if s in unscored)
    run_valid = not (rows and unscored_run / len(rows) > 0.5)
    invalid_reason = (f"{unscored_run}/{len(rows)} scenarios infra/quota "
                      "— environment outage, not a model result"
                      if not run_valid else "")
    partial = len(rows) < len(names)
    if partial:
        run_valid = False
        invalid_reason += ("; " if invalid_reason else "") + (
            f"partial run — {len(rows)}/{len(names)} scenarios "
            "completed (budget or --max-scenarios)")
    table = "\n".join(
        f"| {n} | {s} | {t} | {c} | {d:.0f}s |" for n, s, t, c, d in rows)
    md = (f"# Benchmark `{args.suite}` — {now:%Y-%m-%d %H:%M}\n\n"
          f"**Score: {score}** ({sum(1 for _,s,*_ in rows if s=='pass')} pass, "
          f"{sum(1 for _,s,*_ in rows if s=='flaky')} flaky, "
          f"{sum(1 for _,s,*_ in rows if s=='fail')} fail, "
          f"{sum(1 for _,s,*_ in rows if s in ('infra','quota','skip-quota'))} infra/quota, "
          f"{sum(1 for _,s,*_ in rows if s=='unimplemented')} unimplemented)\n\n"
          f"| scenario | status | turns | tool calls | dur |\n"
          f"|---|---|---|---|---|\n{table}\n")
    if run_metrics:
        md += ("\n## Captured metrics\n\n"
               + "\n".join(f"- `{k}` = {v}"
                           for k, v in sorted(run_metrics.items())) + "\n")
    if not run_valid:
        md += (f"\n> **Invalid run** — {invalid_reason}. Excluded from "
               "trend/baseline comparisons.\n")
    if violations:
        md += ("\n## Policy violations\n\n"
               + "\n".join(f"- {v}" for v in violations) + "\n")
    print("\n" + md)

    if not args.dry_run:
        key = f"benchmark/{args.suite}-{now:%Y%m%d-%H%M%S}"
        _post(f"{args.mddb.rstrip('/')}/add", {
            "collection": COLLECTION, "key": key, "lang": "en",
            "contentMd": md,
            "meta": {"kind": ["benchmark"], "suite": [args.suite],
                     "score": [str(score)],
                     "status": ["fail" if any(s == "fail" for _, s, *_ in rows)
                                else "pass"],
                     "valid": [str(run_valid).lower()],
                     "partial": [str(partial).lower()],
                     "invalid_reason": ([invalid_reason]
                                        if invalid_reason else []),
                     "violations": violations or ["none"],
                     "ts": [now.isoformat(timespec="seconds")],
                     "metrics": [f"{k}={v}"
                                 for k, v in sorted(run_metrics.items())],
                     "scenarios": [f"{n}:{s}" for n, s, *_ in rows]},
        })
        if args.report_cms:
            # Report meta contract (ssot.apps.ada-cms-reports.yml) —
            # validate before the write; a missing field skips the publish.
            bmeta = {"kind": ["page"], "slug": [f"benchmark-{args.suite}"],
                     "title": [f"Benchmark {args.suite} — {now:%Y-%m-%d}"],
                     "format": ["markdown"], "instance": ["tony"],
                     "domain": ["bench"],
                     "summary": [f"{args.suite} score {score} — "
                                 f"{sum(1 for _, s, *_ in rows if s == 'pass')} pass, "
                                 f"{sum(1 for _, s, *_ in rows if s == 'fail')} fail, "
                                 f"{len(violations)} policy violations"
                                 + (" (invalid run)" if not run_valid else "")],
                     "fresh_for": ["1d"],
                     "confidence": ["high" if run_valid else "low"],
                     "timeline": [f"{now.isoformat(timespec='minutes')}: "
                                  f"{args.suite} run, score {score}"],
                     "updated": [now.isoformat(timespec="seconds")]}
            bcheck = validate_report_meta(bmeta)
            for w in bcheck["warnings"]:
                print(f"  meta warning: {w}", file=sys.stderr)
            if bcheck["ok"]:
                _post(f"{args.mddb.rstrip('/')}/add", {
                    "collection": "ada-cms-pages", "key": f"benchmark-{args.suite}",
                    "lang": "en", "contentMd": md,
                    "meta": bmeta,
                })
            else:
                print(f"  benchmark cms page NOT published — meta contract "
                      f"violation: {bcheck['missing']}", file=sys.stderr)

        # Auto-report judgment — deterministic; maintains the 'auto-report'
        # CMS page so report-worthy items are always visible, and appends an
        # auto-report doc to the bank only when something IS worthy.
        prior = _fetch_prior_statuses(args.mddb, args.suite,
                                      now.strftime("%Y%m%d-%H%M%S"))
        worthy = _report_worthy(rows, violations, prior)
        worthy_md = (f"# Auto Report — {now:%Y-%m-%d %H:%M}\n\n")
        if worthy:
            worthy_md += ("Report-worthy items detected in the latest "
                          f"`{args.suite}` suite run (score {score}):\n\n"
                          + "\n".join(f"- **{w}**" for w in worthy) + "\n\n")
        else:
            worthy_md += (f"Nothing report-worthy from the latest "
                          f"`{args.suite}` run (score {score}).\n\n")
        worthy_md += (
            "## How this page works\n\n"
            "Updated automatically by `scenario-benchmark.py` after each suite "
            "run. An item lands here when a scenario FAILs, a policy violation "
            "fires, a scenario regresses vs the previous run of the same "
            "suite, or a scenario stays flaky across runs. Ada reads this page "
            "for 'anything to report?' questions — do not hand-edit; fix the "
            "underlying failure instead.\n")
        wmeta = {"kind": ["page"], "slug": ["auto-report"],
                 "title": ["Auto Report"], "format": ["markdown"],
                 "instance": ["tony"], "domain": ["bench"],
                 "summary": [f"{len(worthy)} report-worthy item(s) from the "
                             f"latest {args.suite} run (score {score})"],
                 "fresh_for": ["1d"],
                 "confidence": ["high"],
                 "timeline": [f"{now.isoformat(timespec='minutes')}: "
                              f"{args.suite} run — {len(worthy)} worthy"],
                 "updated": [now.isoformat(timespec="seconds")],
                 "worthy": [str(len(worthy))]}
        wcheck = validate_report_meta(wmeta)
        for w in wcheck["warnings"]:
            print(f"  meta warning: {w}", file=sys.stderr)
        if wcheck["ok"]:
            _post(f"{args.mddb.rstrip('/')}/add", {
                "collection": "ada-cms-pages", "key": "auto-report", "lang": "en",
                "contentMd": worthy_md,
                "meta": wmeta,
            })
        else:
            print(f"  auto-report page NOT published — meta contract "
                  f"violation: {wcheck['missing']}", file=sys.stderr)
        if worthy:
            _post(f"{args.mddb.rstrip('/')}/add", {
                "collection": COLLECTION,
                "key": f"auto-report/{args.suite}-{now:%Y%m%d-%H%M%S}",
                "lang": "en", "contentMd": worthy_md, "ttl": 30 * 86400,
                "meta": {"kind": ["auto-report"], "suite": [args.suite],
                         "score": [str(score)],
                         "ts": [now.isoformat(timespec="seconds")],
                         "items": worthy},
            })
            print(f"  auto-report: {len(worthy)} worthy item(s)")

        _progress("finished — final doc written")
    return 1 if any(s == "fail" for _, s, *_ in rows) or violations else 0


if __name__ == "__main__":
    sys.exit(main())
