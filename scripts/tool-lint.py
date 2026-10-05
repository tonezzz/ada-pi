#!/usr/bin/env python3
"""Ada tool-surface lint — the audit stage of the tool-consolidation
program (docs/assessments/tool-consolidation-spec-2026-10-04.md).

Reads the declared tool surface WITHOUT importing the backend (AST +
YAML only, so it runs in any CI/validate environment):

  builtin  — dicts with "name": <str> + description/parameters in
             backend/realtime_provider.py
  drop-in  — enabled entries in backend/tools.d/manifest.yml
  aliases  — backend/tool_runner.py::_ALIASES / _ALIAS_ARG_DEFAULTS
  contract — docs/ssot/ssot.tool-surface.yml (cap, families, debt)

Checks (each FAIL exits 1):
  count     declared surface must not exceed ssot count_cap
  descsize  declaration description exceeds ssot desc_max_lines /
            desc_max_chars — prose belongs in backend/tool_guide.yml
            (the error path), not the shipped schema
  dup       a name declared both builtin and in tools.d
  alias     alias key still declared / target undeclared / chain /
            stray alias not in a family's absorbed list /
            _ALIAS_ARG_DEFAULTS row without an _ALIASES row
  absorbed  spec-absorbed name retired from the surface without an
            _ALIASES row to the family canonical
  gates     every *_TOOLS gate member + benchmark.yml write_tools
            entry must resolve to a declared tool or a live alias
  coverage  declared tool referenced by no scenario yaml and absent
            from coverage_debt/coverage_exempt
  family    a family whose merge has started (canonical declared or
            absorbed names retired) must have its scenario file

Usage: tool-lint.py [--repo PATH] [--json] [--verbose]
Exit: 0 clean / 1 violations / 2 setup error (missing inputs).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import yaml

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
# Scenario yaml fields that hold tool names (live suite).
_EXPECT_LIST_FIELDS = ("calls", "calls_any", "no_calls_except")
_EXPECT_FLAG_FIELDS = ("no_calls",)          # bool flag — names not held here
_TOP_LIST_FIELDS = ("needs_tools", "needs_any")
# tool_runner.py set names whose members must resolve (gates preserved).
_GATE_SETS = (
    "CONTROL_TOOLS", "MEMORY_WRITE_TOOLS", "CALENDAR_WRITE_TOOLS",
    "CMS_WRITE_TOOLS", "DEVIN_CONFIRMED_TOOLS", "DOC_TOOLS",
    "DRIVE_TOOLS", "CAPTURE_CONFIRMED_TOOLS", "SECONDARY_BLOCKED_TOOLS",
)


# -- static extraction ------------------------------------------------------

def _str_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(
        node.value, str) else None


def decl_details(path: Path) -> list[dict]:
    """Every declaration dict in a file -> {name, lineno, desc_lines,
    desc_chars}. A declaration dict always carries "name" plus a
    description and/or a parameters schema; parameter sub-dicts keyed
    "name" (e.g. {"name": {"type": "string"}}) have a non-string value
    and are skipped."""
    out: list[dict] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value: v for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        name = _str_const(keys.get("name", ast.Constant(value=None)))
        if not name or not _NAME_RE.match(name):
            continue
        if not ({"description", "parameters", "parameters_json_schema"}
                & keys.keys()):
            continue
        desc_node = keys.get("description")
        desc_lines = desc_chars = 0
        if desc_node is not None:
            desc_lines = (desc_node.end_lineno or desc_node.lineno) \
                - desc_node.lineno + 1
            try:
                desc_chars = len(ast.literal_eval(desc_node))
            except (ValueError, TypeError, SyntaxError):
                desc_chars = -1  # non-literal — can't measure, skip
        out.append({"name": name, "lineno": node.lineno,
                    "desc_lines": desc_lines, "desc_chars": desc_chars})
    return out


def declared_builtin(provider_path: Path) -> dict[str, int]:
    """function_declaration names in realtime_provider.py -> lineno."""
    out: dict[str, int] = {}
    for d in decl_details(provider_path):
        out.setdefault(d["name"], d["lineno"])
    return out


def declared_tools_d(manifest_path: Path) -> dict[str, str]:
    """Enabled tools.d entries -> module name."""
    if not manifest_path.is_file():
        return {}
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    out = {}
    for name, cfg in (data.get("tools") or {}).items():
        cfg = cfg or {}
        if cfg.get("enabled") is False:
            continue
        out[str(name)] = str(cfg.get("module") or name)
    return out


def _eval_set(node: ast.AST, env: dict[str, frozenset]) -> frozenset:
    """Evaluate a set expression: literals, Name refs, | unions."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        vals = set()
        for elt in node.elts:
            c = _str_const(elt)
            if c is not None:
                vals.add(c)
            elif isinstance(elt, ast.Name) and elt.id in env:
                vals |= env[elt.id]
            else:
                vals |= _eval_set(elt, env)
        return frozenset(vals)
    if isinstance(node, ast.Name):
        return env.get(node.id, frozenset())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _eval_set(node.left, env) | _eval_set(node.right, env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
            node.func.id in ("set", "frozenset") and node.args:
        return _eval_set(node.args[0], env)
    return frozenset()


def _eval_str_dict(node: ast.AST) -> dict[str, str]:
    """Evaluate a {str: str} dict literal (Const keys/values only)."""
    out = {}
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            ks, vs = _str_const(k), _str_const(v)
            if ks is not None and vs is not None:
                out[ks] = vs
    return out


def _eval_str_list_dict(node: ast.AST) -> dict[str, dict]:
    """Evaluate a {str: {str: const}} literal (_ALIAS_ARG_DEFAULTS)."""
    out = {}
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            ks = _str_const(k)
            if ks is None or not isinstance(v, ast.Dict):
                continue
            inner = {}
            for ik, iv in zip(v.keys, v.values):
                iks = _str_const(ik)
                if iks is not None and isinstance(iv, ast.Constant):
                    inner[iks] = iv.value
            out[ks] = inner
    return out


def runner_tables(runner_path: Path) -> dict:
    """Pull _ALIASES / _ALIAS_ARG_DEFAULTS / *_TOOLS gate sets out of
    tool_runner.py without importing it."""
    tree = ast.parse(runner_path.read_text(encoding="utf-8"))
    env: dict[str, frozenset] = {}
    aliases: dict[str, str] = {}
    arg_defaults: dict[str, dict] = {}
    for node in ast.walk(tree):
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if t.id == "_ALIASES":
                aliases = _eval_str_dict(value)
            elif t.id == "_ALIAS_ARG_DEFAULTS":
                arg_defaults = _eval_str_list_dict(value)
            elif t.id in _GATE_SETS:
                env[t.id] = _eval_set(value, env)
            elif isinstance(value, (ast.Set, ast.BinOp)):
                env.setdefault(t.id, _eval_set(value, env))
    return {"aliases": aliases, "arg_defaults": arg_defaults,
            "gate_sets": {k: env.get(k, frozenset()) for k in _GATE_SETS}}


def scenario_families(live_driver: Path) -> dict[str, list[str]]:
    """TOOL_FAMILIES from scenario-live.py (@name expansion table)."""
    try:
        tree = ast.parse(live_driver.read_text(encoding="utf-8"))
    except OSError:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name) and node.target.id == "TOOL_FAMILIES":
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return {}
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOL_FAMILIES"
                for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return {}
    return {}


def _expand(names, families):
    out = []
    for n in names or []:
        if isinstance(n, str) and n.startswith("@"):
            out += families.get(n[1:], [])
        else:
            out.append(n)
    return out


def scenario_coverage(scen_dir: Path,
                      families: dict[str, list[str]]) -> dict[str, list[str]]:
    """tool name -> [scenario names] across tests/scenarios-live/*.yaml."""
    covered: dict[str, list[str]] = {}
    for path in sorted(scen_dir.glob("*.yaml")):
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(spec, dict):
            continue
        scn = str(spec.get("name") or path.stem)
        refs = set(_expand(spec.get("needs_tools"), families))
        refs |= set(_expand(spec.get("needs_any"), families))
        for turn in spec.get("turns") or []:
            exp = ((turn or {}).get("expect") or {})
            for f in _EXPECT_LIST_FIELDS:
                refs |= set(_expand(exp.get(f), families))
        for t in refs:
            covered.setdefault(t, []).append(scn)
    return covered


def collect_surface(repo: Path) -> dict:
    """The full census: {declared, builtin, dynamic, aliases, ...}."""
    repo = Path(repo)
    builtin = declared_builtin(repo / "backend/realtime_provider.py")
    dynamic = declared_tools_d(repo / "backend/tools.d/manifest.yml")
    tables = runner_tables(repo / "backend/tool_runner.py")
    families = scenario_families(repo / "scripts/scenario-live.py")
    return {
        "builtin": builtin, "dynamic": dynamic,
        "declared": set(builtin) | set(dynamic),
        "aliases": tables["aliases"],
        "arg_defaults": tables["arg_defaults"],
        "gate_sets": tables["gate_sets"],
        "scenario_families": families,
    }


# -- checks -----------------------------------------------------------------

def lint(repo: Path) -> dict:
    repo = Path(repo)
    ssot_path = repo / "docs/ssot/ssot.tool-surface.yml"
    if not ssot_path.is_file():
        return {"ok": False, "exit": 2,
                "errors": [f"missing {ssot_path}"]}
    ssot = yaml.safe_load(ssot_path.read_text(encoding="utf-8")) or {}
    surf = collect_surface(repo)
    declared, aliases = surf["declared"], surf["aliases"]
    arg_defaults = surf["arg_defaults"]

    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    # -- count cap --
    cap = int(ssot.get("count_cap") or 0)
    target = int(ssot.get("target_count") or 0)
    if cap and len(declared) > cap:
        errors.append(
            f"count: {len(declared)} declared tools exceed cap {cap} — "
            "consolidate or bump docs/ssot/ssot.tool-surface.yml:count_cap "
            "in the same commit")

    # -- description size — prose lives in tool_guide.yml, not the schema --
    # The contract (card ada-tools-desc-slim): a declaration description is
    # a <=2-line routing blurb; operational detail moves to the error path
    # (a "usage" key on failed/denied results). Lines are AST content lines
    # of the description value (paren wrapper excluded).
    max_lines = int(ssot.get("desc_max_lines") or 4)
    max_chars = int(ssot.get("desc_max_chars") or 300)
    desc_files = [(repo / "backend/realtime_provider.py", None)]
    for module in sorted(set(surf["dynamic"].values())):
        mod_path = repo / "backend/tools.d" / f"{module}.py"
        if mod_path.is_file():
            # Only dicts whose name is a manifest-declared tool count —
            # unrelated {"name": ..., "description": ...} dicts in the
            # module are not schema declarations.
            desc_files.append((mod_path, set(surf["dynamic"])))
    for path, names in desc_files:
        for d in decl_details(path):
            if names is not None and d["name"] not in names:
                continue
            over = []
            if d["desc_lines"] > max_lines:
                over.append(f"{d['desc_lines']} lines > {max_lines}")
            if d["desc_chars"] > max_chars:
                over.append(f"{d['desc_chars']} chars > {max_chars}")
            if over:
                errors.append(
                    f"descsize: {d['name']} description is "
                    f"{' and '.join(over)} ({path.name}:{d['lineno']}) — "
                    "the schema ships a <=2-line routing blurb; move prose "
                    "to backend/tool_guide.yml (the error path)")

    # -- duplicate declarations across builtin and tools.d --
    for name in sorted(set(surf["builtin"]) & set(surf["dynamic"])):
        errors.append(
            f"dup: {name} declared in both realtime_provider and tools.d")

    families_ssot = ssot.get("families") or {}
    absorbed_all: dict[str, str] = {}   # absorbed name -> family
    for fam, cfg in families_ssot.items():
        for n in (cfg or {}).get("absorbed") or []:
            absorbed_all[str(n)] = fam

    # -- alias table integrity --
    for old, new in sorted(aliases.items()):
        if old == new:
            errors.append(f"alias: {old} maps to itself")
        if old in declared:
            errors.append(
                f"alias: {old} is still a declared tool — a soft alias "
                "must be off-surface (remove its declaration or its row)")
        if new not in declared:
            errors.append(
                f"alias: {old} -> {new} but {new} is not a declared tool")
        if new in aliases:
            errors.append(
                f"alias: {old} -> {new} chains through another alias — "
                "point at the canonical tool directly")
        if old not in absorbed_all:
            errors.append(
                f"alias: {old} is not in any family's absorbed list — "
                "stray alias not covered by the spec")
    for name in sorted(set(arg_defaults) - set(aliases)):
        errors.append(
            f"alias: _ALIAS_ARG_DEFAULTS has {name} with no _ALIASES row")

    # -- absorbed-name bookkeeping per family --
    for fam, cfg in sorted(families_ssot.items()):
        cfg = cfg or {}
        absorbed = [str(n) for n in cfg.get("absorbed") or []]
        canonical = str(cfg.get("canonical") or "")
        retired = [n for n in absorbed if n not in declared]
        pending = [n for n in absorbed if n in declared]
        merge_started = bool(retired) or (canonical and canonical in declared)
        if pending:
            infos.append(
                f"family {fam}: merge pending — still declared: "
                + ", ".join(sorted(pending)))
        for n in retired:
            if n not in aliases:
                errors.append(
                    f"absorbed: {n} (family {fam}) left the declared "
                    "surface with no _ALIASES row — spec-absorbed names "
                    "must stay callable via the alias table")
            elif canonical and aliases.get(n) != canonical:
                warns.append(
                    f"absorbed: {n} aliases to {aliases.get(n)} not the "
                    f"{fam} canonical {canonical}")
        if merge_started:
            if canonical and canonical not in declared:
                errors.append(
                    f"absorbed: family {fam} is merging but canonical "
                    f"{canonical} is not declared")
            scen = str(cfg.get("scenario") or "")
            if scen:
                scen_path = repo / str(ssot.get("scenario_dir")
                                       or "tests/scenarios-live") \
                    / f"{scen}.yaml"
                if not scen_path.is_file():
                    errors.append(
                        f"family {fam}: merge started but regression "
                        f"scenario {scen_path.name} is missing "
                        "(tools-merge-gate also requires a passing run)")

    # -- gate sets + benchmark policy must keep resolving --
    for set_name, members in sorted(surf["gate_sets"].items()):
        for m in sorted(members):
            if m not in declared and m not in aliases:
                errors.append(
                    f"gates: {set_name} member {m} resolves to nothing — "
                    "not declared and not aliased (gate diluted)")
    bench_path = repo / "tests/benchmark.yml"
    if bench_path.is_file():
        bench = yaml.safe_load(bench_path.read_text(encoding="utf-8")) or {}
        for m in (bench.get("policy") or {}).get("write_tools") or []:
            m = str(m)
            if m not in declared and m not in aliases:
                errors.append(
                    f"gates: benchmark.yml write_tools entry {m} "
                    "resolves to nothing")
        # Scenario ids appear both as file stems (underscored) and as the
        # yaml `name:` field (often hyphenated) — match either spelling.
        scen_names = set()
        for p in (repo / str(ssot.get("scenario_dir")
                               or "tests/scenarios-live")).glob("*.yaml"):
            try:
                spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                spec = {}
            for ident in (p.stem, str(spec.get("name") or "")):
                if ident:
                    scen_names.add(ident)
                    scen_names.add(ident.replace("-", "_"))
        for s in (bench.get("policy") or {}).get("write_allowed_in") or []:
            if str(s) not in scen_names:
                warns.append(
                    f"gates: benchmark.yml write_allowed_in {s} is not a "
                    "known scenario")

    # -- scenario coverage --
    scen_dir = repo / str(ssot.get("scenario_dir") or "tests/scenarios-live")
    covered = scenario_coverage(scen_dir, surf["scenario_families"])
    debt = {str(n) for n in ssot.get("coverage_debt") or []}
    exempt = {str(n) for n in ssot.get("coverage_exempt") or []}
    for t in sorted(declared):
        if t in covered or t in debt or t in exempt:
            continue
        errors.append(
            f"coverage: {t} has no scenario reference and is not in "
            "coverage_debt — add a scenario or list it in "
            "ssot.tool-surface.yml")
    for t in sorted(debt & set(covered)):
        warns.append(
            f"coverage: {t} is covered by {covered[t]} — remove it from "
            "coverage_debt")
    for t in sorted((debt | exempt) - declared):
        warns.append(
            f"coverage: {t} listed in SSOT but not declared — stale entry")
    for t in sorted(set(covered) - declared - set(aliases)):
        if not t.startswith("@"):
            warns.append(
                f"coverage: scenario references undeclared tool {t} "
                f"({', '.join(covered[t])})")

    report = {
        "ok": not errors, "exit": 1 if errors else 0,
        "errors": errors, "warnings": warns, "info": infos,
        "surface": {
            "declared": len(declared),
            "builtin": len(surf["builtin"]),
            "tools_d": len(surf["dynamic"]),
            "cap": cap, "target": target,
            "aliases": len(aliases),
            "covered": len(set(covered) & declared),
            "coverage_debt": len(debt),
        },
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(
        Path(__file__).resolve().parent.parent))
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    ap.add_argument("--verbose", action="store_true",
                    help="print warnings and info, not just failures")
    args = ap.parse_args()

    rep = lint(Path(args.repo))
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return rep["exit"]
    s = rep["surface"] if "surface" in rep else {}
    if s:
        print(f"tool surface: {s['declared']} declared "
              f"({s['builtin']} builtin + {s['tools_d']} tools.d), "
              f"cap {s['cap']}, target {s['target']}, "
              f"{s['aliases']} aliases, {s['covered']} scenario-covered, "
              f"{s['coverage_debt']} debt")
    for e in rep["errors"]:
        print(f"FAIL {e}")
    if args.verbose:
        for w in rep.get("warnings", []):
            print(f"warn {w}")
        for i in rep.get("info", []):
            print(f"info {i}")
    if rep["ok"]:
        print(f"tool-lint: clean ({len(rep.get('warnings', []))} warnings "
              f"— --verbose to list)")
    else:
        print(f"tool-lint: {len(rep['errors'])} violation(s)")
    return rep["exit"]


if __name__ == "__main__":
    sys.exit(main())
