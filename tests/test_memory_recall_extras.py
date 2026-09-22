"""Unit tests for the 2026-09 memory/recall improvements:

- per-instance bank-name placeholders in tool schemas
- cross-bank ('all') search
- draft visibility rules
- conversation query expansion (coreference)
- session priming text
- NotebookLM deep-tier answer cache
- ConversationMemory shared across provider reconnects
"""

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADA_INSTANCE_ID", "test")

from backend import conversation_memory as cm
from backend import memory_ops
from backend.conversation_memory import ConversationMemory
from backend.realtime_provider import _fill_bank_placeholders, create_provider
from tests.scenario_engine import FakeMddb, build_registry


class PlaceholderTests(unittest.TestCase):
    def test_fill_bank_placeholders_recurses(self):
        node = {
            "description": "Banks: {banks}; writable: {writable_banks}",
            "items": ["{banks}", {"deep": "{writable_banks}"}],
            "untouched": 5,
        }
        out = _fill_bank_placeholders(node, "general, personal", "personal")
        self.assertEqual(
            out["description"], "Banks: general, personal; writable: personal"
        )
        self.assertEqual(out["items"][0], "general, personal")
        self.assertEqual(out["items"][1]["deep"], "personal")
        self.assertEqual(out["untouched"], 5)

    def test_shared_conversation_across_providers(self):
        conv = ConversationMemory("s1")
        p1 = create_provider(session_id="s1", conversation=conv)
        p2 = create_provider(session_id="s1", conversation=conv)
        self.assertIs(p1.conversation, conv)
        self.assertIs(p2.conversation, conv)
        p3 = create_provider(session_id="s1")
        self.assertIsNot(p3.conversation, conv)


class ExpandQueryTests(unittest.TestCase):
    def test_coref_expands_with_context(self):
        conv = ConversationMemory("s1")
        conv.add_user("the ada-ha-michael dashboard is blank")
        conv.add_assistant("let me check the runbook")
        out = conv.expand_query("what about the other one?")
        self.assertIn("ada-ha-michael", out)
        self.assertIn("other one", out)

    def test_self_contained_query_unchanged(self):
        conv = ConversationMemory("s1")
        conv.add_user("the ada-ha-michael dashboard is blank")
        q = "how do I restart the ada-ha-michael service on mn01"
        self.assertEqual(conv.expand_query(q), q)

    def test_empty_conversation_no_expansion(self):
        conv = ConversationMemory("s1")
        self.assertEqual(conv.expand_query("it?"), "it?")


class PrimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prime_includes_facts(self):
        fake = FakeMddb()
        reg = build_registry("tony")
        bank = reg.bank("personal")
        await fake.add_document(
            bank.mddb_collection, "personal/gate", "en",
            "The gate remote is on the hook.",
            {"status": ["active"], "kind": ["fact"], "scope": ["tony"]},
        )
        text = await memory_ops.session_prime_text(
            fake, reg, summary="Discussed the gate remote yesterday."
        )
        self.assertIn("Discussed the gate remote", text)
        self.assertIn("gate remote is on the hook", text)
        self.assertIn("(system)", text)

    async def test_prime_none_when_empty(self):
        fake = FakeMddb()
        reg = build_registry("tony")
        self.assertIsNone(
            await memory_ops.session_prime_text(fake, reg, summary=None)
        )

    async def test_prime_disabled_by_env(self):
        fake = FakeMddb()
        reg = build_registry("tony")
        with patch.dict(os.environ, {"ADA_SESSION_PRIME": "0"}):
            self.assertIsNone(
                await memory_ops.session_prime_text(
                    fake, reg, summary="summary"
                )
            )


class NlmCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeMddb()
        self.patcher = patch.object(cm, "_mddb", return_value=self.fake)
        self.patcher.start()
        self.mem = ConversationMemory("s1")

    async def asyncTearDown(self):
        self.patcher.stop()

    async def test_put_then_get(self):
        await self.mem._nlm_cache_put("what is mn01", "infra", "mn01 is the mini PC.")
        out = await self.mem._nlm_cache_get("what is mn01", "infra")
        self.assertEqual(out, "mn01 is the mini PC.")

    async def test_ctx_isolation(self):
        await self.mem._nlm_cache_put("what is mn01", "infra", "answer")
        self.assertIsNone(await self.mem._nlm_cache_get("what is mn01", "memory"))

    async def test_stale_entry_ignored(self):
        coll = cm._nlm_cache_collection()
        await self.fake.add_document(
            coll, "infra-2026-01-01-x", "en", "old answer",
            {"ctx": ["infra"], "created": ["2026-01-01"], "kind": ["nlm-answer"]},
        )
        self.assertIsNone(await self.mem._nlm_cache_get("mn01", "infra"))


class DraftVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_rules(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        reg = build_registry("michael")
        personal = reg.bank("personal")
        doc = {
            "meta": {
                "status": ["draft"],
                "scope": ["michael"],
                "valid_from": [today],
            },
            "score": 0.9,
        }
        self.assertTrue(memory_ops._draft_visible(personal, doc, "michael"))
        self.assertFalse(memory_ops._draft_visible(personal, doc, "tony"))
        low = dict(doc); low["score"] = 0.3
        self.assertFalse(memory_ops._draft_visible(personal, low, "michael"))
        stale = {"meta": {**doc["meta"], "valid_from": ["2020-01-01"]}, "score": 0.9}
        self.assertFalse(memory_ops._draft_visible(personal, stale, "michael"))
        home = reg.bank("home")
        self.assertFalse(memory_ops._draft_visible(home, doc, "michael"))


if __name__ == "__main__":
    unittest.main()
