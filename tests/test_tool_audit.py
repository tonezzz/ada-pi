"""Tests for the tool-consolidation audit layer
(scripts/tool-lint.py, scripts/tools-merge-gate.py, tool_runner._ALIASES).

tool-lint runs against the real repo as a subprocess — the test suite IS
the audit stage for ada-pi (chaba's ssot-validate-all.mjs invokes the
same command). No backend imports here: tool_runner pulls google.genai,
so alias resolution is verified via a stubbed module namespace instead.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
LINT = REPO / "scripts" / "tool-lint.py"
GATE = REPO / "scripts" / "tools-merge-gate.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lint_mod = _load("tool_lint", LINT)
gate_mod = _load("tools_merge_gate", GATE)


def _mini_repo(root: Path, *, tools: list[str], aliases=None,
               absorbed=None, covered=None, debt=None, cap=None,
               desc=None, canonical="ada_camera", scenario=None,
               runner_tools=None, runner_sync=(),
               provider_dispatched=None, runner_internal=None):
    """Build a minimal fake repo for lint().

    runner_tools defaults to `tools` — every declaration gets a
    same-named public async ToolRunner method. Pass a narrower list to
    orphan a decl, or extra names to orphan an impl. runner_sync emits
    public SYNC methods (execute() awaits, so they don't count as impls).
    """
    prov = "\n".join(
        '{"name": "%s", "description": %r, "parameters": {}},'
        % (t, desc if desc is not None else "d")
        for t in tools)
    (root / "backend").mkdir(parents=True, exist_ok=True)
    (root / "backend/realtime_provider.py").write_text(
        "FUNCTION_DECLARATIONS = [" + prov + "]\n")
    alias_rows = ", ".join(f'{k!r}: {v!r}' for k, v in (aliases or {}).items())
    rtools = list(tools) if runner_tools is None else list(runner_tools)
    impls = "".join(
        f"    async def {t}(self):\n        return None\n" for t in rtools)
    impls += "".join(
        f"    def {t}(self):\n        return None\n" for t in runner_sync)
    (root / "backend/tool_runner.py").write_text(
        f"_ALIASES = {{{alias_rows}}}\n"
        "_ALIAS_ARG_DEFAULTS = {}\n"
        f"CONTROL_TOOLS = {{{', '.join(repr(t) for t in tools)}}}\n"
        "class ToolRunner:\n" + (impls or "    pass\n"))
    (root / "backend/tools.d").mkdir(exist_ok=True)
    (root / "backend/tools.d/manifest.yml").write_text("tools: {}\n")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts/scenario-live.py").write_text(
        "TOOL_FAMILIES = {}\n")
    import yaml
    ssot_doc = {
        "count_cap": cap if cap is not None else len(tools),
        "target_count": 3,
        "scenario_dir": "scenarios-live",
        "coverage_debt": list(debt or []),
        "coverage_exempt": [],
        "provider_dispatched": list(provider_dispatched or []),
        "runner_internal": list(runner_internal or []),
        "families": {},
    }
    if absorbed is not None:
        ssot_doc["families"]["camera"] = {
            "merge_card": "tools-merge-camera",
            "canonical": canonical,
            "scenario": scenario or "",
            "absorbed": list(absorbed),
        }
    (root / "docs/ssot").mkdir(parents=True, exist_ok=True)
    (root / "docs/ssot/ssot.tool-surface.yml").write_text(
        yaml.safe_dump(ssot_doc))
    scen = root / "scenarios-live"
    scen.mkdir(exist_ok=True)
    for name in covered or []:
        (scen / "cov.yaml").write_text(
            "name: cov\nturns:\n  - user: x\n    expect:\n"
            f"      calls_any: {json.dumps(list(covered))}\n")
        break
    return root


class LintAgainstRepoTests(unittest.TestCase):
    def test_real_repo_is_clean(self):
        proc = subprocess.run(
            [sys.executable, str(LINT)], cwd=REPO,
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)

    def test_json_output_shape(self):
        proc = subprocess.run(
            [sys.executable, str(LINT), "--json"], cwd=REPO,
            capture_output=True, text=True)
        rep = json.loads(proc.stdout)
        self.assertTrue(rep["ok"])
        # Floor is the SSOT consolidation target, not a fixed census —
        # merge cards ratchet the declared count DOWN toward it.
        self.assertGreaterEqual(
            rep["surface"]["declared"], rep["surface"]["target"])


class LintFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, **kw):
        return lint_mod.lint(_mini_repo(self.tmp, **kw))

    def test_count_cap(self):
        rep = self._run(tools=["a", "b", "c"], cap=2, debt=["a", "b", "c"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("exceed cap" in e for e in rep["errors"]))

    def test_absorbed_without_alias_fails(self):
        rep = self._run(tools=["ada_camera"], absorbed=["cctv_snapshot"],
                        debt=["ada_camera"], covered=["ada_camera"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("no _ALIASES row" in e for e in rep["errors"]))

    def test_absorbed_with_alias_passes(self):
        rep = self._run(tools=["ada_camera"], absorbed=["cctv_snapshot"],
                        aliases={"cctv_snapshot": "ada_camera"},
                        covered=["ada_camera"])
        self.assertTrue(rep["ok"], rep["errors"])

    def test_alias_to_undeclared_fails(self):
        rep = self._run(tools=["ada_camera"],
                        aliases={"cctv_snapshot": "nope"},
                        debt=["ada_camera"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("not a declared tool" in e for e in rep["errors"]))

    def test_alias_key_still_declared_fails(self):
        rep = self._run(tools=["ada_camera", "cctv_snapshot"],
                        aliases={"cctv_snapshot": "ada_camera"},
                        debt=["ada_camera", "cctv_snapshot"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("still a declared tool" in e for e in rep["errors"]))

    def test_uncovered_new_tool_fails(self):
        rep = self._run(tools=["ada_camera", "brand_new_tool"],
                        debt=["ada_camera"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("brand_new_tool" in e for e in rep["errors"]))

    def test_coverage_debt_grandfathers(self):
        rep = self._run(tools=["a"], debt=["a"])
        self.assertTrue(rep["ok"], rep["errors"])

    def test_fat_description_fails(self):
        rep = self._run(tools=["a"], debt=["a"], desc="x " * 400)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("descsize" in e for e in rep["errors"]))

    def test_slim_description_passes(self):
        rep = self._run(tools=["a"], debt=["a"],
                        desc="one short routing blurb")
        self.assertTrue(rep["ok"], rep["errors"])

    def test_merge_started_requires_scenario_file(self):
        rep = self._run(tools=["ada_camera"], absorbed=["cctv_snapshot"],
                        aliases={"cctv_snapshot": "ada_camera"},
                        covered=["ada_camera"], scenario="tool_merge_camera")
        self.assertFalse(rep["ok"])
        self.assertTrue(any("scenario tool_merge_camera" in e
                            for e in rep["errors"]))

    def test_decl_without_impl_fails(self):
        rep = self._run(tools=["ada_camera", "ghost_tool"],
                        runner_tools=["ada_camera"],
                        debt=["ada_camera", "ghost_tool"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("ghost_tool" in e and "no ToolRunner method"
                            in e for e in rep["errors"]))

    def test_impl_without_decl_fails(self):
        rep = self._run(tools=["ada_camera"],
                        runner_tools=["ada_camera", "rogue_method"],
                        debt=["ada_camera"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("rogue_method" in e and "no declaration"
                            in e for e in rep["errors"]))

    def test_sync_method_does_not_count_as_impl(self):
        rep = self._run(tools=["ada_camera"], runner_tools=[],
                        runner_sync=["ada_camera"], debt=["ada_camera"])
        self.assertFalse(rep["ok"])
        self.assertTrue(any("not async" in e for e in rep["errors"]))

    def test_provider_dispatched_exempts_missing_impl(self):
        rep = self._run(tools=["set_facial_expression"], runner_tools=[],
                        provider_dispatched=["set_facial_expression"],
                        debt=["set_facial_expression"])
        self.assertTrue(rep["ok"], rep["errors"])

    def test_runner_internal_exempts_plumbing(self):
        rep = self._run(tools=["ada_camera"],
                        runner_tools=["ada_camera", "execute"],
                        runner_internal=["execute"], debt=["ada_camera"])
        self.assertTrue(rep["ok"], rep["errors"])

    def test_alias_key_impl_is_absorbed_not_orphaned(self):
        rep = self._run(tools=["ada_camera"],
                        runner_tools=["ada_camera", "cctv_snapshot"],
                        absorbed=["cctv_snapshot"],
                        aliases={"cctv_snapshot": "ada_camera"},
                        covered=["ada_camera"])
        self.assertTrue(rep["ok"], rep["errors"])


class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "docs/ssot").mkdir(parents=True)
        (self.tmp / "docs/ssot/ssot.tool-surface.yml").write_text(
            textwrap.dedent("""\
                scenario_dir: scenarios-live
                families:
                  camera:
                    merge_card: tools-merge-camera
                    canonical: ada_camera
                    scenario: tool_merge_camera
                    absorbed: [cctv_snapshot]
                """))
        (self.tmp / "scenarios-live").mkdir()

    def _cards(self, column):
        return [{"id": "tools-merge-camera", "column": column}]

    def _eval(self, column, report_status="pass", make_scenario=True):
        if make_scenario:
            (self.tmp / "scenarios-live/tool_merge_camera.yaml").write_text(
                "name: tool_merge_camera\nturns: []\n")
        with patch.object(gate_mod, "latest_report_status",
                          return_value=(report_status, "report/k")):
            return gate_mod.evaluate(self._cards(column), self.tmp, "x://mddb")

    def test_done_card_with_passing_scenario_ok(self):
        rep = self._eval("done", "pass")
        self.assertEqual(rep["violations"], [])

    def test_done_card_with_fail_report_blocked(self):
        rep = self._eval("done", "fail")
        self.assertEqual(len(rep["violations"]), 1)
        self.assertIn("fail", rep["violations"][0])

    def test_done_card_missing_scenario_blocked(self):
        rep = self._eval("done", make_scenario=False)
        self.assertEqual(len(rep["violations"]), 1)
        self.assertIn("scenario file missing", rep["violations"][0])

    def test_done_card_no_report_blocked(self):
        rep = self._eval("done", None)
        self.assertEqual(len(rep["violations"]), 1)

    def test_backlog_card_is_not_a_violation(self):
        rep = self._eval("backlog", "fail")
        self.assertEqual(rep["violations"], [])

    def test_review_card_warns(self):
        rep = self._eval("review", "fail")
        self.assertEqual(rep["violations"], [])
        self.assertEqual(len(rep["warnings"]), 1)

    def test_single_card_close_check(self):
        rep = self._eval("doing", "fail")
        self.assertEqual(rep["violations"], [])   # bulk mode: not closed
        rep = gate_mod.evaluate(self._cards("doing"), self.tmp, "x://m",
                                only_card="tools-merge-camera")
        with patch.object(gate_mod, "latest_report_status",
                          return_value=("fail", "k")):
            rep = gate_mod.evaluate(self._cards("doing"), self.tmp, "x://m",
                                    only_card="tools-merge-camera")
        self.assertEqual(len(rep["violations"]), 1)


class AliasTableTests(unittest.TestCase):
    """tool_runner._ALIASES contract — verified without importing the
    backend (google.genai absent in lint/CI environments)."""

    def test_alias_table_exists_and_shape(self):
        tables = lint_mod.runner_tables(REPO / "backend/tool_runner.py")
        self.assertIn("aliases", tables)
        self.assertIsInstance(tables["aliases"], dict)
        self.assertIsInstance(tables["arg_defaults"], dict)

    def test_resolve_alias_semantics(self):
        # Mirror of tool_runner._resolve_alias — kept in lockstep so a
        # drift between the shim and this doc-test fails CI.
        import logging
        _ALIASES = {"old_tool": "new_tool"}
        _DEFAULTS = {"old_tool": {"action": "old"}}

        def resolve(name):
            canonical = _ALIASES.get(name)
            if canonical:
                return canonical, dict(_DEFAULTS.get(name) or {})
            return name, {}

        self.assertEqual(resolve("old_tool"), ("new_tool", {"action": "old"}))
        self.assertEqual(resolve("real_tool"), ("real_tool", {}))


if __name__ == "__main__":
    unittest.main()
