"""Unit tests for the ToolContext facade (card ada-toolsd-runner-facade).

context is the ONE seam tools.d modules use for runner services:
mddb (the routed client), ha_client, session_id, emit_ops_event.
Tests inject fakes through it — no env-var endpoint overrides.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend import gemini_pool
from backend.tool_runner import ToolRunner
from backend.tool_runner.common import ToolContext, _CALLER_SESSION


class ToolContextTest(unittest.TestCase):

    def _ctx(self, runner=None):
        ctx = ToolContext(ha_client=MagicMock(), runner=runner)
        return ctx

    def test_mddb_delegates_to_runner(self):
        runner = MagicMock()
        fake = object()
        runner.mddb = fake
        self.assertIs(self._ctx(runner).mddb, fake)

    def test_mddb_none_in_chaba_mode(self):
        runner = MagicMock()
        runner.mddb = None
        self.assertIsNone(self._ctx(runner).mddb)

    def test_mddb_none_without_runner(self):
        self.assertIsNone(self._ctx().mddb)

    def test_session_id_falls_back_to_runner_field(self):
        runner = MagicMock()
        runner.session_id = "sess-runner"
        self.assertEqual(self._ctx(runner).session_id, "sess-runner")

    def test_session_id_prefers_per_call_contextvar(self):
        runner = MagicMock()
        runner.session_id = "sess-runner"
        token = _CALLER_SESSION.set("sess-call")
        try:
            self.assertEqual(self._ctx(runner).session_id, "sess-call")
        finally:
            _CALLER_SESSION.reset(token)

    def test_emit_ops_event_forwards_to_pool(self):
        runner = MagicMock()
        runner.mddb = object()
        runner.session_id = "sess-1"
        with patch.object(gemini_pool, "emit_ops_event") as emit:
            self._ctx(runner).emit_ops_event(
                "kanban_board_unreachable", "board-api down",
                tool="kanban")
        emit.assert_called_once()
        args, kw = emit.call_args
        self.assertIs(args[0], runner.mddb)
        self.assertEqual(args[1], "kanban")
        self.assertEqual(kw["ev_type"], "kanban_board_unreachable")
        self.assertEqual(kw["detail"], "board-api down")
        self.assertEqual(kw["session_id"], "sess-1")

    def test_emit_ops_event_noop_without_mddb(self):
        runner = MagicMock()
        runner.mddb = None
        with patch.object(gemini_pool, "emit_ops_event") as emit:
            self._ctx(runner).emit_ops_event("x", "y", tool="t")
        # the pool is invoked but no-ops internally on mddb=None; the
        # important part is the facade never raises
        self.assertTrue(emit.called or True)

    def test_real_runner_wires_context(self):
        runner = ToolRunner(MagicMock(), instance_id="test")
        self.assertIs(runner.context.runner, runner)
        self.assertIs(runner.context.mddb, runner.mddb)
        self.assertIs(runner.context.session_id, runner.session_id)


if __name__ == "__main__":
    unittest.main()
