#!/usr/bin/env python3
"""Live-scenario runner + MDDB report sink.

Runs tests/scenarios-live/*.yaml through scripts/scenario-live.py, one
doc per scenario in the 'ada-ha-scenario-reports' collection (outside the
bank registry — never reaches ada_memory_search).

Scenario yaml fields used here:
  tier: smoke|full   (default 'full' — only 'smoke' runs under --tier smoke)
  url: <ws url>      per-scenario ws target (e.g. ws://127.0.0.1:8003/ws for
                     michael); absent → --url / ADA_LIVE_URL.
  env_file: <path>   dotenv file to read ADA_API_KEY from (e.g. another
                     instance's env mounted under /secrets). Overridden by
                     key_name when both are set.
  key_name: <issued key>  resolved from the keys file (--keys-file);
                        dict entries may carry 'device' → passed as
                        device_id ws param. Absent → --api-key env/flag.
  params: {...}      extra ws query params (merged after device_id)

Usage:
  scenario-report.py [--tier smoke|full] [--url WS] [--mddb URL]
                     [--keys-file PATH] [--scenarios-dir PATH] [--dry-run]

Env fallbacks: ADA_LIVE_URL, MDDB_BASE_URL, ADA_API_KEY, ADA_KEYS_FILE.
Exit code: 1 when any scenario fails (after one retry — a pass-on-retry
is recorded as 'flaky').
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)  # so 'backend.event_log' resolves from any cwd
COLLECTION = "ada-ha-scenario-reports"
REPORT_TTL_DAYS = 14


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


def _post(url: str, payload: dict) -> bool:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as exc:
        print(f"  report write failed: {exc}", file=sys.stderr)
        return False


def _report(mddb: str, scenario: str, status: str, tier: str,
            transcript: str, runs: int) -> bool:
    key = f"report/{scenario}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    valid_until = (
        datetime.date.today() + datetime.timedelta(days=REPORT_TTL_DAYS)
    ).isoformat()
    return _post(f"{mddb.rstrip('/')}/add", {
        "collection": COLLECTION, "key": key, "lang": "en",
        "contentMd": f"# {scenario} — {status}\n\n{transcript}",
        "meta": {
            "kind": ["report"], "subject": ["scenario-live"],
            "scenario": [scenario], "status": [status], "tier": [tier],
            "runs": [str(runs)],
            "last_verified": [datetime.date.today().isoformat()],
            "valid_until": [valid_until],
        },
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="smoke", choices=["smoke", "full"])
    ap.add_argument("--url", default=os.environ.get("ADA_LIVE_URL") or "ws://127.0.0.1:8002/ws")
    ap.add_argument("--mddb", default=os.environ.get("MDDB_BASE_URL") or "http://127.0.0.1:11023/v1")
    ap.add_argument("--keys-file", default=os.environ.get("ADA_KEYS_FILE") or "")
    ap.add_argument("--api-key", default=os.environ.get("ADA_API_KEY") or "")
    ap.add_argument("--scenarios-dir", default=os.path.join(REPO, "tests", "scenarios-live"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    keys = _keys(args.keys_file) if args.keys_file else {}
    driver = os.path.join(HERE, "scenario-live.py")
    results: list[tuple[str, str]] = []

    import glob as _glob
    pattern = os.path.join(args.scenarios_dir, "*.yaml")
    for path in sorted(_glob.glob(pattern)):
        spec = yaml.safe_load(open(path).read()) or {}
        name = spec.get("name") or os.path.basename(path)
        tier = spec.get("tier") or "full"
        if args.tier == "smoke" and tier != "smoke":
            continue

        url = spec.get("url") or args.url
        api_key = args.api_key
        params = dict(spec.get("params") or {})
        if spec.get("env_file"):
            api_key = _env_value(str(spec["env_file"]), "ADA_API_KEY") or api_key
        if spec.get("key_name"):
            api_key, device = _key_entry(keys, spec["key_name"])
            if device and "device_id" not in params:
                params["device_id"] = device
            if not api_key:
                print(f"== {name}: SKIP (key {spec['key_name']} not in keys file)")
                results.append((name, "skip"))
                continue
        if params:
            import urllib.parse
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(params)

        status, out, runs = "fail", "", 0
        for attempt in (1, 2):
            runs = attempt
            cmd = [sys.executable, driver, path, "--url", url]
            if api_key:
                cmd += ["--api-key", api_key]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=900,
            )
            out = proc.stdout + proc.stderr
            if proc.returncode == 0:
                status = "flaky" if attempt == 2 else "pass"
                break

        print(f"== {name}: {status} ({runs} run{'s' if runs > 1 else ''})")
        if not args.dry_run:
            _report(args.mddb, name, status, tier, out[-6000:], runs)
        try:
            from backend.event_log import log_event
            log_event("scenario-run", name, args.tier,
                      f"{status} in {runs} run{'s' if runs > 1 else ''}")
        except Exception:
            pass
        results.append((name, status))

    failed = [n for n, s in results if s == "fail"]
    print(f"\n{len(results)} scenarios: "
          + ", ".join(f"{n}={s}" for n, s in results) or "none")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
