"""Tests for session context-awareness features:

- raw transcript persistence to the local archive dir
- agenda + pending-proposal injection at session start
- action-proposal extraction and resolution (ada-ha-actions-*)
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADA_INSTANCE_ID", "test")

from backend import conversation_memory as cm
from backend.conversation_memory import ConversationMemory
from backend.realtime_provider import GeminiLiveProvider


class FakeMddb:
    """In-memory MDDB stand-in: captures writes, serves stored docs."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.adds: list[dict] = []
        self.updates: list[dict] = []

    async def add_document(self, collection, key, lang, content_md, meta=None,
                           timeout=None):
        doc = {"key": key, "contentMd": content_md, "meta": meta or {}}
        self.docs[key] = doc
        self.adds.append({"collection": collection, "key": key,
                          "content_md": content_md, "meta": meta})
        return doc

    async def get_document(self, collection, key, lang="en"):
        return self.docs.get(key)

    async def search_documents(self, collection, query="*", filter_meta=None,
                               limit=10):
        out = []
        for d in self.docs.values():
            meta = d.get("meta") or {}
            if filter_meta:
                ok = all(
                    any(v in (meta.get(k) or []) for v in vals)
                    for k, vals in filter_meta.items()
                )
                if not ok:
                    continue
            out.append(d)
        return out[:limit]

    async def update_document(self, collection, key, lang="en",
                              content_md=None, meta=None):
        doc = self.docs.get(key)
        if doc is None:
            return None
        if meta is not None:
            doc["meta"] = meta
        self.updates.append({"key": key, "meta": meta})
        return doc


class TranscriptFileTests(unittest.IsolatedAsyncioTestCase):
    def test_transcript_file_written(self):
        with tempfile.TemporaryDirectory() as td:
            conv = ConversationMemory("sess-1")
            conv.add_user("hello ada")
            conv.add_assistant("hi tony")
            with patch.object(cm, "TRANSCRIPT_DIR", td):
                conv._save_transcript_file()
            files = list(Path(td).glob("*.md"))
            self.assertEqual(len(files), 1)
            body = files[0].read_text()
            self.assertIn("sess-1", body)
            self.assertIn("hello ada", body)
            self.assertIn("hi tony", body)

    def test_transcript_save_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            conv = ConversationMemory("sess-2")
            conv.add_user("hi")
            with patch.object(cm, "TRANSCRIPT_DIR", td), \
                    patch.dict(os.environ, {"ADA_TRANSCRIPT_SAVE": "0"}):
                conv._save_transcript_file()
            self.assertEqual(list(Path(td).glob("*.md")), [])

    async def test_persist_writes_file_without_notebooklm(self):
        """Transcript + summary pipeline run even when NotebookLM is off."""
        with tempfile.TemporaryDirectory() as td:
            conv = ConversationMemory("sess-3")
            conv.client = MagicMock()
            conv.client.configured = False
            conv.add_user("remember the wifi password is on the fridge")
            with patch.object(cm, "TRANSCRIPT_DIR", td), \
                    patch.object(ConversationMemory, "_update_summary",
                                 new=AsyncMock()):
                await conv.persist()
            self.assertEqual(len(list(Path(td).glob("*.md"))), 1)
            # persist scheduled the summary/candidate/action pipeline task
            task = cm._summary_cache.get("task")
            self.assertIsNotNone(task)
            await task  # drains the AsyncMock coroutine


class AgendaContextTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, svc):
        runner = MagicMock()
        runner.calendar = svc
        return GeminiLiveProvider(tool_runner=runner, session_id="t")

    async def test_agenda_includes_events_and_tasks(self):
        svc = MagicMock()
        svc.plan_day = AsyncMock(return_value={
            "events": [{"title": "Dentist", "start": "2026-09-22T15:00:00+07:00",
                        "all_day": False}],
            "open_tasks": [{"title": "Buy milk", "due": "2026-09-22"}],
            "errors": [],
        })
        with patch.object(cm, "pending_action_proposals",
                          new=AsyncMock(return_value=[])):
            tail = await self._provider(svc)._session_context_tail()
        self.assertIn("Today's agenda", tail)
        self.assertIn("15:00 Dentist", tail)
        self.assertIn("open task: Buy milk", tail)

    async def test_agenda_no_calendar_returns_empty(self):
        svc_none = None
        tail = await self._provider(svc_none)._session_context_tail()
        self.assertEqual(tail, "")

    async def test_agenda_disabled_by_env(self):
        svc = MagicMock()
        svc.plan_day = AsyncMock(side_effect=AssertionError("must not run"))
        with patch.dict(os.environ, {"ADA_AGENDA_CONTEXT": "0"}):
            tail = await self._provider(svc)._session_context_tail()
        self.assertEqual(tail, "")

    async def test_agenda_survives_plan_failure(self):
        svc = MagicMock()
        svc.plan_day = AsyncMock(side_effect=RuntimeError("provider down"))
        with patch.object(cm, "pending_action_proposals",
                          new=AsyncMock(return_value=[])):
            tail = await self._provider(svc)._session_context_tail()
        self.assertEqual(tail, "")

    async def test_pending_proposals_listed(self):
        svc = MagicMock()
        svc.plan_day = AsyncMock(return_value={
            "events": [], "open_tasks": [], "errors": [],
        })
        props = [{"key": "action-2026-09-22-s1-0",
                  "text": "event: Dentist — Thursday 15:00"}]
        with patch.object(cm, "pending_action_proposals",
                          new=AsyncMock(return_value=props)):
            tail = await self._provider(svc)._session_context_tail()
        self.assertIn("Pending suggestions", tail)
        self.assertIn("action-2026-09-22-s1-0", tail)
        self.assertIn("Dentist", tail)


class ActionProposalTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_actions_writes_pending(self):
        conv = ConversationMemory("sess-x")
        fake = FakeMddb()
        resp = MagicMock()
        resp.text = json.dumps([
            {"type": "event", "title": "Dentist", "when": "Thursday 15:00"},
            {"type": "task", "title": "Buy milk"},
        ])
        genai_client = MagicMock()
        genai_client.aio.models.generate_content = AsyncMock(
            return_value=resp)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
                patch.object(cm, "_GEMINI_API_KEY", "x"), \
                patch.object(cm, "_mddb", lambda: fake), \
                patch("google.genai.Client", return_value=genai_client):
            await conv._extract_actions("user: dentist thursday 3pm")
        self.assertEqual(len(fake.adds), 2)
        self.assertEqual(fake.adds[0]["collection"], "ada-ha-actions-test")
        self.assertEqual(fake.adds[0]["meta"]["status"], ["pending"])
        self.assertEqual(fake.adds[0]["meta"]["kind"], ["action-proposal"])
        self.assertEqual(fake.adds[0]["meta"]["action_type"], ["event"])
        self.assertIn("Dentist", fake.adds[0]["content_md"])

    async def test_pending_proposals_reads_collection(self):
        fake = FakeMddb()
        await fake.add_document("ada-ha-actions-test", "k1", "en",
                                "task: Buy milk",
                                meta={"status": ["pending"],
                                      "kind": ["action-proposal"]})
        await fake.add_document("ada-ha-actions-test", "k2", "en",
                                "task: Old",
                                meta={"status": ["applied"],
                                      "kind": ["action-proposal"]})
        with patch.object(cm, "_mddb", lambda: fake):
            props = await cm.pending_action_proposals()
        self.assertEqual([p["key"] for p in props], ["k1"])

    async def test_resolve_marks_status(self):
        fake = FakeMddb()
        await fake.add_document("ada-ha-actions-test", "k1", "en",
                                "task: Buy milk",
                                meta={"status": ["pending"],
                                      "kind": ["action-proposal"]})
        with patch.object(cm, "_mddb", lambda: fake):
            out = await cm.resolve_action_proposal("k1", "applied")
        self.assertEqual(out, "marked applied")
        self.assertEqual(fake.docs["k1"]["meta"]["status"], ["applied"])

    async def test_resolve_rejects_bad_resolution(self):
        with patch.object(cm, "_mddb", lambda: FakeMddb()):
            out = await cm.resolve_action_proposal("k1", "bogus")
        self.assertIn("applied", out)

    async def test_resolve_via_tool_runner(self):
        from backend.tool_runner import ToolRunner
        runner = ToolRunner(AsyncMock(), instance_id="test")
        with patch("backend.conversation_memory.resolve_action_proposal",
                   new=AsyncMock(return_value="marked dismissed")) as m:
            out = await runner.execute(
                "ada_resolve_action", {"key": "k1", "resolution": "dismissed"})
        self.assertEqual(out, "marked dismissed")
        m.assert_awaited_once_with("k1", "dismissed")


if __name__ == "__main__":
    unittest.main()
