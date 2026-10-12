"""Unit tests for backend/tools.d/conv_journal.py — the tool file is
loaded by path (tools.d is not a package)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "conv_journal.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.conv_journal_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("LINE_MEDIA_ARCHIVE_DIR", str(tmp_path))
    return _load()


def _seed(archive: Path, chat="C822room", day="2026-10-12"):
    d = archive / chat
    d.mkdir(parents=True)
    (d / f"journal-{day}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"ts": "2026-10-12T10:00", "dir": "in", "from": "KK",
             "kind": "text", "text": "itinerary attached"},
            {"ts": "2026-10-12T10:00:05", "dir": "in", "from": "KK",
             "kind": "image",
             "url": "https://x.sslip.io/relay-img/arch-a",
             "message_id": "m1", "mime": "image/jpeg"},
            {"ts": "2026-10-12T10:00:30", "dir": "out",
             "from": "Kaewta", "kind": "text",
             "text": "safari day noted"},
        ]) + "\n")
    (d / "a9f3c2e1b4d5.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    return d


class _MDDB:
    def __init__(self):
        self.writes = []

    async def update_document(self, collection, key, lang="en",
                              content_md=None, meta=None, tool="",
                              **_kw):
        self.writes.append((collection, key, content_md, meta))
        return {"ok": True}


def _runner(mddb):
    return SimpleNamespace(context=SimpleNamespace(mddb=mddb))


@pytest.mark.asyncio
async def test_list_empty(tool, tmp_path):
    r = await tool.run(_runner(None), action="list")
    assert r["ok"] and r["chats"] == []


@pytest.mark.asyncio
async def test_build_writes_report(tool, tmp_path):
    _seed(tmp_path)
    mddb = _MDDB()
    r = await tool.run(_runner(mddb), action="build",
                       chat="C822room", date="2026-10-12")
    assert r["ok"], r
    assert r["entries"] == 3
    coll, key, body, meta = mddb.writes[0]
    assert coll == tool.CMS_COLLECTION  # lane-forced collection
    assert key == "conv-c822room-2026-10-12"
    assert "**KK** sent image" in body
    assert "![]" in body and "relay-img/arch-a" in body
    assert "safari day noted" in body
    assert meta["kind"] == ["report"]
    assert "public" not in meta  # never site-published


@pytest.mark.asyncio
async def test_build_defaults_first_chat(tool, tmp_path):
    _seed(tmp_path)
    mddb = _MDDB()
    r = await tool.run(_runner(mddb), action="build",
                       date="2026-10-12")
    assert r["ok"] and mddb.writes[0][1] == "conv-c822room-2026-10-12"


@pytest.mark.asyncio
async def test_build_missing_day(tool, tmp_path):
    _seed(tmp_path)
    r = await tool.run(_runner(_MDDB()), action="build",
                       chat="C822room", date="2026-01-01")
    assert not r["ok"] and "action='list'" in r["error"]


@pytest.mark.asyncio
async def test_build_no_mddb(tool, tmp_path):
    _seed(tmp_path)
    r = await tool.run(_runner(None), action="build",
                       chat="C822room", date="2026-10-12")
    assert not r["ok"] and "mddb" in r["error"]


@pytest.mark.asyncio
async def test_build_bad_date(tool, tmp_path):
    r = await tool.run(_runner(_MDDB()), action="build",
                       chat="C822room", date="12/10/2026")
    assert not r["ok"] and "YYYY-MM-DD" in r["error"]
