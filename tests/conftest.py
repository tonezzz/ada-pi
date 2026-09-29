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

os.environ["ADA_INSTANCE_ID"] = "test"
os.environ.setdefault("GEMINI_API_KEY", "x")
