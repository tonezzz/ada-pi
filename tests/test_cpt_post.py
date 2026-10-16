"""Unit tests for backend/tools.d/cpt_post.py — the tool file is loaded
by path (tools.d is not a package). WP REST is mocked throughout; these
cover classification inputs, title/slug/meta shaping, person defaulting,
and the plugin-skew meta retry — not the wire."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "cpt_post.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.cpt_post_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool():
    return _load()


def _runner(instance="kk", identity=None):
    r = SimpleNamespace(instance=instance)
    r._memory_identity = lambda: identity
    return r


class TestTitle:
    def test_expense_composes_vendor_date(self, tool):
        assert tool._title("expense", "",
                           {"vendor": "7-Eleven",
                            "receipt_date": "2026-10-15"}) == \
            "Expense — 7-Eleven 2026-10-15"

    def test_expense_falls_back_to_name_then_receipt(self, tool):
        assert tool._title("expense", "fuel", {}) == "Expense — fuel"
        assert tool._title("expense", "", {}) == "Expense — receipt"

    def test_labeled_kinds(self, tool):
        assert tool._title("contact", "Somsak Plumber", {}) == \
            "Contact — Somsak Plumber"
        assert tool._title("reminder", "pay AIS bill", {}) == \
            "Reminder — pay AIS bill"
        assert tool._title("asset", "CR-V", {}) == "Asset — CR-V"


class TestSlug:
    def test_ascii(self, tool):
        assert tool._slug("contact", "Honda Service") == \
            "contact-honda-service"

    def test_thai_hash_fallback_stable(self, tool):
        a = tool._slug("contact", "ช่างสมศักดิ์")
        assert a == tool._slug("contact", "ช่างสมศักดิ์")
        assert a.startswith("contact-") and len(a) == len("contact-") + 8

    def test_kinds_dont_collide(self, tool):
        assert tool._slug("asset", "x") != tool._slug("contact", "x")


class TestPerson:
    def test_explicit_wins(self, tool):
        assert tool._person(_runner("kk"), "nhong") == "nhong"

    def test_lane_member_default(self, tool):
        assert tool._person(_runner("kk"), "") == "kk"

    def test_dev_lane_base(self, tool):
        assert tool._person(_runner("kk-dev"), "") == "kk"

    def test_caller_identity_fallback(self, tool):
        assert tool._person(_runner("dev", "person.nhong"), "") == "nhong"
        assert tool._person(_runner("dev", "user-tony"), "") == "tony"

    def test_unmappable_empty(self, tool):
        assert tool._person(_runner("dev", "someone-else"), "") == ""


class TestMeta:
    def test_kind_scoped_and_saved_by(self, tool):
        meta, dropped = tool._collect_meta(
            "expense", "kk", {"amount": "320", "phone": "081",
                              "vendor": "PTT"})
        assert meta == {"amount": "320", "vendor": "PTT",
                        "saved_by": "kk"}
        assert dropped == ["phone"]

    def test_empty_values_ignored(self, tool):
        meta, _ = tool._collect_meta("contact", "", {"phone": "  "})
        assert meta == {}


@pytest.mark.asyncio
class TestRun:
    async def test_bad_kind(self, tool):
        res = await tool.run(_runner(), kind="post", title="x")
        assert res["ok"] is False and "wp_post" in res["error"]

    async def test_title_required_for_entity_kinds(self, tool):
        res = await tool.run(_runner(), kind="contact")
        assert res["ok"] is False and "title" in res["error"]

    async def test_expense_without_title_ok_but_needs_env(self, tool):
        # Stub env empty — hosts with a real wp-boards.env would otherwise
        # post a live record (observed: junk "Expense - receipt" on prod).
        with patch.object(tool.wp, "env", return_value={}):
            res = await tool.run(_runner(), kind="expense", amount="5")
        assert res["ok"] is False
        assert "wp-boards.env" in res["error"]

    async def test_happy_path_expense(self, tool):
        env = {"WP_SITE_URL": "https://x", "WP_APP_USER": "u",
               "WP_APP_PASSWORD": "p"}
        created = {}

        async def fake_create(client, e, title, body, **kw):
            created.update(kw, title=title)
            return {"ok": True, "id": 501, "link": "https://x/?p=501"}

        with patch.object(tool.wp, "env", return_value=env), \
             patch.object(tool.wp, "create_post",
                          side_effect=fake_create):
            res = await tool.run(
                _runner("kk"), kind="expense", vendor="PTT",
                amount="320", receipt_date="2026-10-15")
        assert res["ok"] and res["id"] == 501
        assert created["post_type"] == "expense"
        assert created["meta"]["currency"] == "THB"
        assert created["meta"]["saved_by"] == "kk"
        assert created["slug"].startswith("expense-")
        assert res["title"] == "Expense — PTT 2026-10-15"

    async def test_meta_skew_retry(self, tool):
        """chaba-boards <0.5.1 lacks saved_by: a meta 400 retries
        without saved_by first — the other registered keys survive."""
        env = {"WP_SITE_URL": "https://x", "WP_APP_USER": "u",
               "WP_APP_PASSWORD": "p"}
        calls = []

        async def fake_create(client, e, title, body, **kw):
            calls.append(kw)
            meta = kw.get("meta") or {}
            if "saved_by" in meta:
                return {"ok": False,
                        "error": "post create failed 400: "
                                 "rest_invalid_meta saved_by"}
            return {"ok": True, "id": 77, "link": "https://x/?p=77"}

        with patch.object(tool.wp, "env", return_value=env), \
             patch.object(tool.wp, "create_post",
                          side_effect=fake_create):
            res = await tool.run(_runner(), kind="expense",
                                 vendor="PTT", amount="10")
        assert res["ok"] and "saved_by" not in res["meta_written"]
        assert res["meta_written"] == ["amount", "currency", "vendor"]
        assert len(calls) == 2
        assert "saved_by" not in calls[1]["meta"]
        assert "<0.5.1" in (res["note"] or "")

    async def test_contact_upsert_updates_existing(self, tool):
        env = {"WP_SITE_URL": "https://x", "WP_APP_USER": "u",
               "WP_APP_PASSWORD": "p"}

        class GetResp:
            status_code = 200
            text = ""
            def json(self):
                return [{"id": 42}]

        class PostResp:
            status_code = 200
            text = ""
            def json(self):
                return {"id": 42, "link": "https://x/contact/honda"}

        posts = []

        async def fake_api(client, e, method, route, **kw):
            if method == "GET":
                return GetResp()
            posts.append((route, kw))
            return PostResp()

        with patch.object(tool.wp, "env", return_value=env), \
             patch.object(tool.wp, "api", side_effect=fake_api):
            res = await tool.run(
                _runner("kk"), kind="contact", title="Honda Service",
                phone="02123456")
        assert res["ok"] and res["updated"] and res["id"] == 42
        route, kw = posts[0]
        assert route == "contact/42"
        assert kw["json"]["meta"]["phone"] == "02123456"
        assert kw["json"]["meta"]["saved_by"] == "kk"

    async def test_expense_never_upserts(self, tool):
        env = {"WP_SITE_URL": "https://x", "WP_APP_USER": "u",
               "WP_APP_PASSWORD": "p"}
        api_calls = []

        async def fake_api(client, e, method, route, **kw):
            api_calls.append(route)
            raise AssertionError("GET slug lookup must not run "
                                 "for expense")

        async def fake_create(client, e, title, body, **kw):
            return {"ok": True, "id": 9, "link": "https://x/?p=9"}

        with patch.object(tool.wp, "env", return_value=env), \
             patch.object(tool.wp, "api", side_effect=fake_api), \
             patch.object(tool.wp, "create_post",
                          side_effect=fake_create):
            res = await tool.run(_runner(), kind="expense",
                                 vendor="7-Eleven", amount="45")
        assert res["ok"] and res["created"] and not api_calls
