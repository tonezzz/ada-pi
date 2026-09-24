import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from backend import devin_dispatch as dd


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _patch_exec(captured: list, stdout: bytes = b"ok\n"):
    async def fake_exec(*cmd, **kwargs):
        captured.append(cmd)
        return _FakeProc(stdout)

    return patch("asyncio.create_subprocess_exec", fake_exec)


class RunQuotingTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_command_shell_quotes_args(self):
        captured: list = []
        with _patch_exec(captured):
            await dd._run("start", "chaba", "Investigate (sensor.nobito_pm2_5) stuck; check logs")
        cmd = captured[0]
        # ssh args: ssh -o BatchMode=yes -o ConnectTimeout=10 HOST <remote_cmd>
        remote_cmd = cmd[-1]
        self.assertTrue(remote_cmd.startswith(dd.BIN + " start chaba '"))
        self.assertIn("(sensor.nobito_pm2_5)", remote_cmd)
        self.assertIn("'", remote_cmd)
        # BIN keeps its leading ~ unquoted so the remote shell expands it.
        self.assertNotIn("'" + dd.BIN, remote_cmd)


class DispatchDedupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        dd._recent_dispatches.clear()

    async def test_similar_retry_reuses_task_id(self):
        with _patch_exec([], stdout=b"20260924-073049-task-a\n"):
            first = await dd.dispatch(
                "chaba",
                "Investigate why the Nobito PM2.5 sensor reading has been "
                "stuck at 72.0. Check history trends, device availability, "
                "and Home Assistant integration logs.",
            )
        with _patch_exec([], stdout=b"should-not-run\n") as _:
            with patch.object(dd, "_run", wraps=dd._run) as run_spy:
                second = await dd.dispatch(
                    "chaba",
                    "Investigate stuck Nobito PM2.5 sensor reading. "
                    "Check history, availability, and logs.",
                )
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(run_spy.call_count, 0)

    async def test_different_task_still_dispatches(self):
        with _patch_exec([], stdout=b"task-a\n"):
            await dd.dispatch("chaba", "Investigate stuck Nobito PM2.5 sensor reading.")
        with _patch_exec([], stdout=b"task-b\n"):
            second = await dd.dispatch("chaba", "Check the Rika RK600 weather station battery.")
        self.assertEqual(second["task_id"], "task-b")
        self.assertNotIn("deduplicated", second)

    def _status_line(self, task_id: str) -> str:
        return (f"{task_id:<42} inactive success    repo=chaba          "
                "transcript=2026-09-24 07:52:21\n")

    async def test_remote_dupe_reuses_recent_task(self):
        # Empty in-process cache (e.g. after a backend restart) — the remote
        # status list still catches a retry of a recently-started task.
        recent = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        tid = f"{recent}-investigate-why-the-nobito-pm2"
        with patch.object(dd, "_run", new=AsyncMock(return_value=self._status_line(tid))):
            res = await dd.dispatch(
                "chaba", "Investigate stuck Nobito PM2.5 sensor reading.")
        self.assertEqual(res["task_id"], tid)
        self.assertTrue(res["deduplicated"])

    async def test_remote_dupe_ignores_old_task(self):
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=dd.DEDUP_WINDOW_S + 60)
               ).strftime("%Y%m%d-%H%M%S")
        tid = f"{old}-investigate-why-the-nobito-pm2"
        calls = []

        async def fake_run(*args):
            calls.append(args)
            return self._status_line(tid) if args[0] == "status" else "task-new\n"

        with patch.object(dd, "_run", new=fake_run):
            res = await dd.dispatch(
                "chaba", "Investigate stuck Nobito PM2.5 sensor reading.")
        self.assertEqual(res["task_id"], "task-new")
        self.assertNotIn("deduplicated", res)
        self.assertEqual(calls[1][0], "start")

    async def test_remote_dupe_ignores_other_repo(self):
        recent = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        tid = f"{recent}-investigate-why-the-nobito-pm2"
        status_out = self._status_line(tid).replace("repo=chaba", "repo=ada-pi")
        calls = []

        async def fake_run(*args):
            calls.append(args)
            return status_out if args[0] == "status" else "task-new\n"

        with patch.object(dd, "_run", new=fake_run):
            res = await dd.dispatch(
                "chaba", "Investigate stuck Nobito PM2.5 sensor reading.")
        self.assertEqual(res["task_id"], "task-new")
        self.assertNotIn("deduplicated", res)


class StatusTrimTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_caps_to_latest(self):
        lines = "\n".join(f"2026092{i}-000000-task-{i} inactive success repo=chaba"
                          for i in range(12)) + "\n"
        with patch.object(dd, "_run", new=AsyncMock(return_value=lines)):
            out = await dd.status()
        self.assertIn(f"showing latest {dd.STATUS_MAX_LINES} of 12", out)
        self.assertIn("task-11", out)
        self.assertNotIn("task-0\n", out)

    async def test_single_task_not_trimmed(self):
        payload = "20260924-073049-task-a inactive success repo=chaba\n"
        with patch.object(dd, "_run", new=AsyncMock(return_value=payload)) as run_spy:
            out = await dd.status("20260924-073049-task-a")
        self.assertEqual(out, payload)
        run_spy.assert_called_once_with("status", "20260924-073049-task-a")


if __name__ == "__main__":
    unittest.main()
