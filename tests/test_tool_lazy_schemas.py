"""Lazy tool schemas — card ada-lazy-tool-schemas (2026-10-13,
arg-name skeleton follow-up ada-lazy-schemas-arg-names 2026-10-14).

Two halves under test:

- realtime_provider ships {name, blurb, arg-name skeleton} for cold
  tools: {"type":"object","properties":{"slug":{},...},"required":[...]}
  — property keys and required survive so the model can shape the call;
  per-property type/description/enum and nested bulk are stripped.
  (FULL_SCHEMA_TOOLS keeps full schemas; ADA_LAZY_SCHEMAS=0 is the kill
  switch, ADA_FULL_SCHEMA_TOOLS extends the hot set.)
- ToolRunner validates args against the REAL declaration schema
  server-side and returns ok:false + the tool_guide usage hint on a miss
  — the self-repair path that makes lazy declarations safe.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import backend.realtime_provider as rp
import backend.tool_runner.common as common
from backend.tool_runner import ToolRunner

EMPTY = {"type": "object", "properties": {}}


def _types_ctx():
    """dep_stubs tolerance: a fabricated google.genai.types namespace may
    lack real enum classes — decl building only needs the attribute to
    EXIST (the enum value never reaches these assertions), so swap in a
    duck-typed stand-in. With real google-genai (or the full dep_stubs
    surface) this is a no-op."""
    if hasattr(rp.types, "Behavior"):
        return contextlib.nullcontext()
    return patch.object(rp, "types", MagicMock())


class LazyDeclTests(unittest.TestCase):
    def test_cold_tools_ship_arg_skeleton(self):
        with _types_ctx():
            full = {d["name"]: d for d in rp._builtin_declarations()}
            decls = rp._apply_lazy_schemas(list(full.values()))
        cold = [d for d in decls if d["name"] not in rp.FULL_SCHEMA_TOOLS]
        self.assertGreater(len(cold), 20)
        for d in cold:
            schema = d["parameters_json_schema"]
            # exact skeleton shape — no schema bulk leaks onto the wire
            self.assertTrue(
                set(schema) <= {"type", "properties", "required"},
                d["name"])
            self.assertEqual(schema.get("type"), "object", d["name"])
            props = schema.get("properties")
            self.assertIsInstance(props, dict, d["name"])
            for pname, pdef in props.items():
                self.assertEqual(pdef, {}, f"{d['name']}.{pname}")
            real = full[d["name"]]["parameters_json_schema"]
            self.assertEqual(
                set(props), set(real.get("properties") or {}), d["name"])
            self.assertEqual(
                schema.get("required") or [],
                real.get("required") or [], d["name"])
            self.assertTrue(d.get("description"), d["name"])

    def test_cold_decl_keeps_arg_names_and_required(self):
        """The regression fix: a cold decl keeps property KEYS + required
        (the model can shape its call) but no type/description/enum."""
        with _types_ctx():
            decls = rp._apply_lazy_schemas(rp._builtin_declarations())
        rem = next(d for d in decls if d["name"] == "ada_forget")
        self.assertEqual(rem["parameters_json_schema"], {
            "type": "object",
            "properties": {k: {} for k in (
                "bank", "key", "reason", "confirmed", "confirm_token")},
            "required": ["bank", "key"],
        })

    def test_trip_calendar_wire_skeleton(self):
        """The reported regression end-to-end on the wire surface:
        trip_calendar's tools.d decl now ships its arg names — Gemini
        Live gets {'slug','start','end',...} to fill instead of an empty
        properties object that invites a {} call."""
        from backend import tools_loader
        decls = rp._apply_lazy_schemas(
            tools_loader.registry().declarations(set()))
        trip = next(d for d in decls if d["name"] == "trip_calendar")
        self.assertEqual(trip["parameters"], {
            "type": "object",
            "properties": {k: {} for k in (
                "slug", "start", "end", "title", "keywords", "public",
                "confirmed")},
        })

    def test_hot_tools_keep_full_schema(self):
        with _types_ctx():
            decls = rp._apply_lazy_schemas(rp._builtin_declarations())
        by_name = {d["name"]: d for d in decls}
        hot = [n for n in rp.FULL_SCHEMA_TOOLS if n in by_name]
        self.assertEqual(sorted(hot), sorted(
            n for n in rp.FULL_SCHEMA_TOOLS if n != "kanban"))
        for n in hot:
            props = by_name[n]["parameters_json_schema"].get("properties")
            self.assertTrue(props or n == "get_rk600_weather", n)
            self.assertNotEqual(
                by_name[n]["parameters_json_schema"], EMPTY, n)

    def test_kill_switch_ships_full_schemas(self):
        with _types_ctx():
            decls = rp._builtin_declarations()
        with patch.dict(os.environ, {"ADA_LAZY_SCHEMAS": "0"}):
            out = rp._apply_lazy_schemas(decls)
        self.assertIs(out, decls)

    def test_env_extend_keeps_extra_full(self):
        with _types_ctx(), patch.dict(
                os.environ, {"ADA_FULL_SCHEMA_TOOLS": "home_search"}):
            out = rp._apply_lazy_schemas(rp._builtin_declarations())
        hs = next(d for d in out if d["name"] == "home_search")
        self.assertIn("kind", hs["parameters_json_schema"]["properties"])

    def test_both_schema_keys_skeletonized(self):
        real = {"type": "object",
                "properties": {"x": {"type": "string",
                                     "description": "ex",
                                     "enum": ["a", "b"]},
                               "y": {"type": "object",
                                     "properties": {"z": {}}}},
                "required": ["x"],
                "additionalProperties": False}
        skeleton = {"type": "object",
                    "properties": {"x": {}, "y": {}},
                    "required": ["x"]}
        fd_json = {"name": "cold_a", "parameters_json_schema": dict(real)}
        fd_plain = {"name": "cold_b", "parameters": dict(real)}
        out = rp._apply_lazy_schemas([fd_json, fd_plain])
        self.assertEqual(out[0]["parameters_json_schema"], skeleton)
        self.assertEqual(out[1]["parameters"], skeleton)

    def test_no_required_omits_key(self):
        fd = {"name": "cold_c",
              "parameters_json_schema": {"type": "object",
                                         "properties": {"x": {}},
                                         "required": []}}
        out = rp._apply_lazy_schemas([fd])
        self.assertEqual(out[0]["parameters_json_schema"],
                         {"type": "object",
                          "properties": {"x": {}}})

    def test_builtin_declarations_fresh_copies(self):
        with _types_ctx():
            a, b = rp._builtin_declarations(), rp._builtin_declarations()
        a[0]["name"] = "mutated"
        self.assertNotEqual(b[0]["name"], "mutated")

    def test_declared_schemas_covers_surface(self):
        with _types_ctx():
            schemas = rp.declared_tool_schemas()
            builtin = rp._builtin_declarations()
        for d in builtin:
            self.assertIn(d["name"], schemas)
        # CAMERA_DECLARATION is appended outside the builtin list but is
        # part of the wire surface — it must validate too.
        self.assertIn("ada_camera_snapshot", schemas)
        # Builtin wins the chaba re-decl collision — the superset keeps
        # habit-write args legal even though the guest decl omits them.
        self.assertIn(
            "challenge_id", schemas["ada_remember"]["properties"])

    def test_hot_set_matches_ssot(self):
        import yaml
        from pathlib import Path
        ssot = yaml.safe_load((Path(__file__).resolve().parents[1]
                               / "docs/ssot/ssot.tool-surface.yml")
                              .read_text())
        self.assertEqual(
            set(ssot["lazy_schemas"]["full_schema_tools"]),
            set(rp.FULL_SCHEMA_TOOLS))


class SchemaValidationTests(unittest.IsolatedAsyncioTestCase):
    """execute() rejects arg-shape misses against the REAL schema and
    answers with the repair payload (usage hint + expected_args) before
    any gate or method runs."""

    SCHEMA = {
        "probe_tool": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "mode": {"type": "string", "enum": ["a", "b"]},
                "n": {"type": "number"},
                "items": {"type": "array"},
            },
            "required": ["q"],
            "additionalProperties": False,
        },
        "loose_tool": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            # no additionalProperties — unknown keys tolerated unless the
            # caller got EVERY name wrong (the total shape miss).
        },
    }

    def setUp(self):
        self.runner = ToolRunner(AsyncMock(), instance_id="test")
        self.runner.probe_tool = AsyncMock(
            return_value={"ok": True, "ran": True})
        self.runner.loose_tool = AsyncMock(
            return_value={"ok": True, "ran": True})

    async def _call(self, name, args):
        with patch.object(common, "_declared_schemas",
                          return_value=self.SCHEMA):
            return await self.runner.execute(name, args)

    async def test_missing_required_rejected(self):
        out = await self._call("probe_tool", {"mode": "a"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "SchemaError")
        self.assertIn("'q'", out["error"])
        self.assertIn("q", out["expected_args"])
        self.runner.probe_tool.assert_not_called()

    async def test_empty_args_schema_error(self):
        """The {} call the v1 empty-schema wire surface produced still
        repairs: missing required -> SchemaError + expected_args before
        the method runs."""
        out = await self._call("probe_tool", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "SchemaError")
        self.assertIn("q", out["expected_args"])
        self.runner.probe_tool.assert_not_called()

    async def test_unknown_arg_rejected(self):
        out = await self._call("probe_tool", {"q": "x", "quarry": "y"})
        self.assertFalse(out["ok"])
        self.assertIn("'quarry'", out["error"])
        self.runner.probe_tool.assert_not_called()

    async def test_usage_hint_attached_on_miss(self):
        with patch.object(common, "_TOOL_GUIDE",
                          {"probe_tool": "q=<the question>"}):
            out = await self._call("probe_tool", {})
        self.assertEqual(out.get("usage"), "q=<the question>")

    async def test_enum_miss_lists_valid_values(self):
        out = await self._call("probe_tool", {"q": "x", "mode": "z"})
        self.assertFalse(out["ok"])
        self.assertIn('"a"', out["error"])
        self.assertIn('"b"', out["error"])

    async def test_structural_type_miss(self):
        out = await self._call("probe_tool", {"q": "x", "items": "nope"})
        self.assertFalse(out["ok"])
        self.assertIn("items", out["error"])

    async def test_scalar_spelling_tolerated(self):
        # '3' for a number passes validation — the method coerces, same
        # as before lazy schemas.
        out = await self._call("probe_tool", {"q": "x", "n": "3"})
        self.assertTrue(out.get("ok"))
        self.runner.probe_tool.assert_awaited_once()

    async def test_good_args_dispatch(self):
        out = await self._call(
            "probe_tool", {"q": "x", "mode": "a", "n": 3})
        self.assertTrue(out.get("ok"))
        self.runner.probe_tool.assert_awaited_once_with(q="x", mode="a", n=3)

    async def test_all_unknown_rejected_without_addprops(self):
        out = await self._call("loose_tool", {"name": "x", "when": "y"})
        self.assertFalse(out["ok"])
        self.assertIn("slug", out["expected_args"])

    async def test_partial_unknown_tolerated_without_addprops(self):
        out = await self._call("loose_tool", {"slug": "x", "extra": 1})
        self.assertTrue(out.get("ok"))
        self.runner.loose_tool.assert_awaited_once()

    async def test_empty_args_on_empty_schema_ok(self):
        out = await self._call("probe_tool", {"q": "x"})
        self.assertTrue(out.get("ok"))

    async def test_alias_implied_args_skip_validation(self):
        """Absorbed-name implied defaults are server-internal contract:
        ada_daily_summary injects period='digest' — deliberately not in
        plan_day's declared enum — and must still reach the method, not
        die on the schema gate. (Model calls only use canonical wire
        names, which ARE validated.)"""
        out = await self.runner.execute(
            "ada_daily_summary", {"day": "yesterday"})
        self.assertNotEqual(out.get("error_type"), "SchemaError")

    async def test_schema_retry_bound_second_miss(self):
        """One corrected retry per window (card
        ada-error-truthfulness-narration): the first SchemaError returns
        the plain repair payload; the second same-tool miss inside the
        window is flagged no_retry so the model narrates the real error
        instead of guessing a third shape."""
        first = await self._call("probe_tool", {})
        self.assertFalse(first["ok"])
        self.assertNotIn("no_retry", first)
        second = await self._call("probe_tool", {"quarry": "y"})
        self.assertEqual(second["error_type"], "SchemaError")
        self.assertTrue(second["no_retry"])
        self.assertIn("DO NOT RETRY", second["error"])
        # A different tool's first miss still gets a clean retry budget.
        other = await self._call("loose_tool", {"when": "y"})
        self.assertEqual(other["error_type"], "SchemaError")
        self.assertNotIn("no_retry", other)

    async def test_schema_retry_bound_cleared_on_success(self):
        """A clean call resets the repair budget — a later miss in the
        same window gets a fresh corrected retry, not a stale flag."""
        await self._call("probe_tool", {})
        ok = await self._call("probe_tool", {"q": "x"})
        self.assertTrue(ok.get("ok"))
        again = await self._call("probe_tool", {})
        self.assertEqual(again["error_type"], "SchemaError")
        self.assertNotIn("no_retry", again)

    async def test_real_schema_trip_calendar(self):
        """End-to-end against the real schema map — the card's trip
        scenario call shape. A total shape miss repairs with
        expected_args + the tool_guide hint; the correct call passes
        validation (the tool itself may then fail on the missing
        generator, which is fine)."""
        schemas = common._declared_schemas()
        if "trip_calendar" not in schemas:
            self.skipTest("declared schema map unavailable")
        bad = await self.runner.execute(
            "trip_calendar", {"trip_slug": "x", "dates": "2030"})
        self.assertEqual(bad["error_type"], "SchemaError")
        self.assertIn("slug", bad["expected_args"])
        self.assertEqual(
            bad.get("usage"), common._tool_guide().get("trip_calendar"))
        good = await self.runner.execute(
            "trip_calendar",
            {"slug": "smoke-nonexistent-trip",
             "start": "2030-01-01", "end": "2030-01-03"})
        self.assertNotEqual(good.get("error_type"), "SchemaError")


if __name__ == "__main__":
    unittest.main()
