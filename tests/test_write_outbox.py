"""Durable write outbox: failed mddb writes park in pending.jsonl, retry
with bounded backoff, deliver on recovery, and dead-letter (with a
user-facing notice) once the window is exhausted."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from backend import write_outbox
from backend.mddb_client import MddbClient


def _client(handler):
    client = MddbClient(base_url="http://mddb.test/v1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _boom(request):
    raise httpx.ConnectError("refused")


def _ok(request):
    return httpx.Response(200, json={"ok": True})


def _read_jsonl(path: Path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _IsolatedDir(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.box = write_outbox.WriteOutbox(Path(self._tmp.name))
        # Tests drive flush_once explicitly — no background retry task.
        self.box._ensure_loop = lambda: None
        self._default = write_outbox._default
        write_outbox._default = self.box
        self.addCleanup(self._restore)

    def _restore(self):
        write_outbox._default = self._default

    @property
    def dir(self) -> Path:
        return Path(self._tmp.name)


class EnqueueTest(_IsolatedDir):

    async def test_failed_durable_add_queues(self):
        res = await _client(_boom).add_document(
            "coll", "key1", "en", "body", meta={"kind": ["x"]},
            durable=True, tool="cms_publish_page", session_id="s1")
        self.assertTrue(write_outbox.is_queued(res))
        pending = _read_jsonl(self.dir / "pending.jsonl")
        self.assertEqual(len(pending), 1)
        e = pending[0]
        self.assertEqual(e["op"], "add")
        self.assertEqual(e["collection"], "coll")
        self.assertEqual(e["key"], "key1")
        self.assertEqual(e["tool"], "cms_publish_page")
        self.assertEqual(e["session_id"], "s1")

    async def test_failed_non_durable_add_returns_none(self):
        res = await _client(_boom).add_document("coll", "k", "en", "b")
        self.assertIsNone(res)
        self.assertEqual(self.box.pending_count(), 0)

    async def test_durable_update_queues_on_refusal(self):
        # get_document fails during the outage -> meta-only refusal queues
        res = await _client(_boom).update_document(
            "coll", "key1", meta={"status": ["answered"]},
            durable=True, tool="devin_answer")
        self.assertTrue(write_outbox.is_queued(res))
        e = _read_jsonl(self.dir / "pending.jsonl")[0]
        self.assertEqual(e["op"], "update")
        self.assertEqual(e["meta"], {"status": ["answered"]})

    async def test_durable_delete_queues(self):
        res = await _client(_boom).delete_document(
            "coll", "key1", durable=True, tool="cms_delete_page")
        self.assertTrue(write_outbox.is_queued(res))


class RetryTest(_IsolatedDir):

    def _force_due(self):
        path = self.dir / "pending.jsonl"
        entries = _read_jsonl(path)
        for e in entries:
            e["next_retry_at"] = 0
        path.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    async def test_retry_delivers_and_clears(self):
        calls = []

        def flaky(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("refused")
            return _ok(request)

        client = _client(flaky)
        res = await client.add_document(
            "coll", "key1", "en", "body", durable=True, tool="t")
        self.assertTrue(write_outbox.is_queued(res))
        self._force_due()
        await self.box.flush_once(client)
        self.assertEqual(self.box.pending_count(), 0)
        self.assertEqual(len(calls), 2)  # original + replay

    async def test_backoff_reschedules(self):
        await _client(_boom).add_document(
            "coll", "key1", "en", "body", durable=True, tool="t")
        self._force_due()
        await self.box.flush_once(_client(_boom))
        e = _read_jsonl(self.dir / "pending.jsonl")[0]
        self.assertEqual(e["attempts"], 1)
        self.assertGreater(e["next_retry_at"], time.time())

    async def test_dead_letter_after_window(self):
        await _client(_boom).add_document(
            "coll", "key1", "en", "body", durable=True, tool="t")
        path = self.dir / "pending.jsonl"
        entries = _read_jsonl(path)
        entries[0]["enqueued_ts"] = time.time() - (write_outbox.WINDOW_S + 60)
        entries[0]["next_retry_at"] = 0
        path.write_text(json.dumps(entries[0]) + "\n", encoding="utf-8")
        await self.box.flush_once(_client(_boom))
        self.assertEqual(self.box.pending_count(), 0)
        dead = _read_jsonl(self.dir / "dead-letters.jsonl")
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["key"], "key1")
        notices = self.box.drain_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("could not be saved", notices[0])
        self.assertEqual(self.box.drain_notices(), [])  # drained once

    async def test_update_replay_reruns_merge(self):
        # Original update fails on the write; replay must re-read the doc
        # (merge-onto-fresh) rather than resend the stale payload.
        gets = []
        adds = []

        def handler(request):
            body = json.loads(request.content)
            if request.url.path == "/v1/get":
                gets.append(body)
                return httpx.Response(200, json={
                    "key": "key1", "meta": {"old": ["1"]},
                    "contentMd": "old body"})
            adds.append(body)
            if len(adds) == 1:
                raise httpx.ConnectError("refused")
            return _ok(request)

        client = _client(handler)
        res = await client.update_document(
            "coll", "key1", meta={"new": ["2"]},
            durable=True, tool="t")
        self.assertTrue(write_outbox.is_queued(res))
        self._force_due()
        await self.box.flush_once(client)
        self.assertEqual(self.box.pending_count(), 0)
        self.assertEqual(len(gets), 2)  # original merge + replay merge

    async def test_survives_restart(self):
        # A new WriteOutbox on the same dir (post-restart) sees the entry.
        await _client(_boom).add_document(
            "coll", "key1", "en", "body", durable=True, tool="t")
        box2 = write_outbox.WriteOutbox(self.dir)
        self.assertEqual(box2.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
