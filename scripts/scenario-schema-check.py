#!/usr/bin/env python3
"""Scenario-file schema check — the cheap structural gate for
tests/scenarios-live/*.yaml (card ada-pi-ci).

scenario-live.py fails a malformed scenario at run time, hours after the
push that broke it. This check runs in CI on every push: yaml.safe_load
each file and require the contract the engine and tier selection rely
on:

  name   non-empty string — the scenario id reported to MDDB/digests
  tier   non-empty string — scenario-report.py --tier filter key
         (missing tier silently defaults to "full"; make it explicit)
  turns  non-empty list of non-empty mappings — turn shapes are open
         (user/audio/speech/reconnect/vcast_display/http_check/...)
         so only the structure is enforced, not the step vocabulary

A file that parses but misses a key is a silent suite hole: it still
loads in the live runner yet can never be selected correctly. Fail
loud here instead.

Usage: scenario-schema-check.py [--scenarios-dir PATH] [--json]
Exit: 0 clean / 1 violations / 2 setup error (missing inputs).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIRED = ("name", "tier", "turns")


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        import yaml
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # yaml.YAMLError or unreadable file
        return [f"parse: {type(exc).__name__}: {exc}"]
    if not isinstance(spec, dict):
        return ["top level is not a mapping"]
    for key in REQUIRED:
        if key not in spec:
            errors.append(f"missing required key {key!r}")
    if "name" in spec and not str(spec.get("name") or "").strip():
        errors.append("name is empty")
    if "tier" in spec and not str(spec.get("tier") or "").strip():
        errors.append("tier is empty")
    turns = spec.get("turns")
    if "turns" in spec:
        if not isinstance(turns, list) or not turns:
            errors.append("turns must be a non-empty list")
        else:
            for i, turn in enumerate(turns):
                if not isinstance(turn, dict) or not turn:
                    errors.append(f"turns[{i}] is not a non-empty mapping")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios-dir",
                    default=str(REPO / "tests" / "scenarios-live"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    scen_dir = Path(args.scenarios_dir)
    if not scen_dir.is_dir():
        print(f"scenario-schema-check: {scen_dir} is not a directory",
              file=sys.stderr)
        return 2

    failures: dict[str, list[str]] = {}
    files = sorted(scen_dir.glob("*.yaml"))
    if not files:
        print(f"scenario-schema-check: no *.yaml under {scen_dir}",
              file=sys.stderr)
        return 2
    for path in files:
        errs = check_file(path)
        if errs:
            failures[path.name] = errs

    if args.json:
        print(json.dumps({"files": len(files), "failures": failures},
                         indent=1))
    else:
        for name, errs in failures.items():
            for err in errs:
                print(f"FAIL {name}: {err}")
        print(f"scenario-schema-check: {len(files)} files, "
              f"{len(failures)} with violations")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
