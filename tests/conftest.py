"""Deterministic test environment — imported before any test module.

Individual test files historically did os.environ.setdefault() at import,
so results depended on pytest's collection order and the caller's shell
(e.g. test_ops_events expects 'tony', test_context_awareness 'test').
Pin the values here; tests that need another instance patch.dict their
own env instead of relying on ambient order.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# google-genai + fastapi are runtime deps, not guaranteed in test/gate
# envs — stub them before any test module's imports run (no-op when the
# real packages are installed).
from tests import dep_stubs
dep_stubs.install()

os.environ["ADA_INSTANCE_ID"] = "test"
os.environ.setdefault("GEMINI_API_KEY", "x")
# unit tests must never append to the real usage ledger (t-ctx rows were
# polluting data/usage.jsonl on every pytest run)
os.environ["ADA_USAGE_LOG"] = "off"

# pytest-asyncio is a venv nicety, not present in bare gate interpreters —
# without it every module-level `async def test_` fails with "async def
# functions are not natively supported". Minimal fallback runner; real
# pytest-asyncio (deploy venv) takes over when installed.
try:
    import pytest_asyncio  # noqa: F401
except ImportError:
    import asyncio
    import inspect

    def pytest_pyfunc_call(pyfuncitem):
        fn = pyfuncitem.obj
        if not inspect.iscoroutinefunction(fn):
            return None
        args = {name: pyfuncitem.funcargs[name]
                for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(fn(**args))
        return True
