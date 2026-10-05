#!/usr/bin/env python3
"""Tool-scenario coverage: every declared tool should be referenced by at
least one scenario in tests/. Prints a per-tool matrix; --fail exits 1
when coverage drops below --min (default: every declared tool covered).

Usage: tool-coverage-report.py [--min 1.0] [--json]
"""
import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "backend" / "realtime_provider.py"
SCEN_DIR = ROOT / "tests"

_SKIP = {"name", "type", "string", "description", "object", "enum",
         "array", "boolean", "integer", "number", "null"}


def declared_tools() -> list[str]:
    src = PROVIDER.read_text()
    names = re.findall(r'"name":\s*"([a-z_]+)"', src)
    tools = sorted({n for n in names if n not in _SKIP})
    # drop-ins (backend/tools.d/)
    manifest = ROOT / "backend" / "tools.d" / "manifest.yml"
    if manifest.exists():
        import yaml
        m = yaml.safe_load(manifest.read_text()) or {}
        tools += sorted((m.get("tools") or {}).keys())
    return sorted(set(tools))


def corpus() -> str:
    out = []
    for f in glob.glob(str(SCEN_DIR / "scenarios-live" / "*.yaml")):
        out.append(Path(f).read_text())
    for f in glob.glob(str(SCEN_DIR / "*.yml")):
        out.append(Path(f).read_text())
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=1.0,
                    help="fraction of tools that must be covered (0-1)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    tools = declared_tools()
    corp = corpus()
    missing = [t for t in tools if not re.search(rf"\b{t}\b", corp)]
    cov = 1 - len(missing) / max(len(tools), 1)

    if a.json:
        print(json.dumps({"total": len(tools), "covered": len(tools) - len(missing),
                          "coverage": round(cov, 3), "missing": missing}))
    else:
        print(f"{len(tools)-len(missing)}/{len(tools)} tools covered "
              f"({cov:.0%}) — min {a.min:.0%}")
        for t in missing:
            print(f"  MISSING  {t}")
    return 0 if cov >= a.min else 1


if __name__ == "__main__":
    sys.exit(main())
