"""Offline scenario tests — every tests/scenarios/*.yaml runs against a
ToolRunner wired to an in-memory FakeMddb (see scenario_engine.py)."""

import unittest
from pathlib import Path

from tests.scenario_engine import run_scenario

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def runTest(self):  # pragma: no cover - replaced by dynamic tests
        pass


def _make(path: Path):
    async def test(self):
        report = await run_scenario(path)
        self.assertTrue(
            report["ok"],
            msg="\n" + "\n".join(report["failures"])
            + f"\n\nsteps: {[s['step'] for s in report['steps']]}",
        )

    return test


for _path in sorted(SCENARIOS_DIR.glob("*.yaml")):
    setattr(ScenarioTests, f"test_{_path.stem.replace('-', '_')}", _make(_path))


if __name__ == "__main__":
    unittest.main()
