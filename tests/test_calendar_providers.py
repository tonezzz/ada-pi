import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx

from backend.calendar_providers import (
    CalendarError,
    CalendarEvent,
    CalendarService,
    Task,
    day_range,
    parse_day,
    split_qualified,
    _parse_when,
)
from backend.google_calendar import GoogleCalendarProvider
from backend.tool_runner import ToolRunner

TZ = ZoneInfo("Asia/Bangkok")


def _registry(providers, read="aggregate", write="google"):
    return {"calendar": {"providers": providers, "read": read, "write": write}}


def _write_registry(spec):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(spec, f)
    f.close()
    return f.name


class FakeProvider:
    """In-memory CalendarProvider + TasksProvider for service tests."""

    def __init__(self, name, events=None, tasks=None, fail=False):
        self.name = name
        self.events = events or []
        self.tasks = tasks or []
        self.fail = fail
        self.deleted = []
        self.created = []
        self.completed = []

    async def _maybe_fail(self):
        if self.fail:
            raise CalendarError(f"{self.name} is down")

    async def list_calendars(self):
        return [{"id": "primary", "title": f"{self.name} cal", "primary": True}]

    async def list_events(self, start, end, query=None):
        await self._maybe_fail()
        return list(self.events)

    async def create_event(self, calendar, title, start, end, notes=None, location=None):
        self.created.append((calendar, title, start, end))
        return CalendarEvent(
            id=f"{calendar or 'primary'}/new1", title=title,
            start=str(start), end=str(end), provider=self.name,
            calendar=calendar or "primary",
        )

    async def delete_event(self, raw_id):
        self.deleted.append(raw_id)

    async def freebusy(self, start, end):
        return [{"calendar": "primary", "start": start.isoformat(), "end": end.isoformat()}]

    async def list_task_lists(self):
        return [{"id": "@default", "title": "Tasks"}]

    async def list_tasks(self, task_list=None, due_before=None):
        await self._maybe_fail()
        return list(self.tasks)

    async def add_task(self, title, due=None, notes=None, task_list=None):
        t = Task(id="@default/t1", title=title, provider=self.name, task_list=task_list or "@default",
                 due=due.isoformat() if due else None, notes=notes)
        self.tasks.append(t)
        return t

    async def complete_task(self, raw_id):
        self.completed.append(raw_id)


def _ev(title, start, cal="primary"):
    return CalendarEvent(id=f"{cal}/{title}", title=title, start=start, end=start,
                         provider="", calendar=cal)


class ParseTests(unittest.TestCase):
    def test_parse_day_keywords_and_iso(self):
        today = datetime.now(TZ).date()
        self.assertEqual(parse_day("today", TZ), today)
        self.assertEqual(parse_day("tomorrow", TZ), today + timedelta(days=1))
        self.assertEqual(parse_day("yesterday", TZ), today - timedelta(days=1))
        self.assertEqual(parse_day("2026-10-05", TZ), date(2026, 10, 5))
        self.assertEqual(parse_day(None, TZ), today)

    def test_parse_day_rejects_junk(self):
        with self.assertRaises(CalendarError):
            parse_day("next fridayish", TZ)

    def test_day_range_local_midnight(self):
        start, end = day_range(date(2026, 9, 22), 1, TZ)
        self.assertEqual(start.isoformat(), "2026-09-22T00:00:00+07:00")
        self.assertEqual((end - start).days, 1)

    def test_day_range_clamps(self):
        start, end = day_range(date(2026, 9, 22), 365, TZ)
        self.assertEqual((end - start).days, 31)

    def test_parse_when_date_and_datetime(self):
        self.assertEqual(_parse_when("2026-09-22", TZ), date(2026, 9, 22))
        dt = _parse_when("2026-09-22T14:00:00", TZ)
        self.assertEqual(dt.tzinfo, TZ)  # naive -> local tz
        dt2 = _parse_when("2026-09-22T14:00:00+09:00", TZ)
        self.assertEqual(dt2.utcoffset(), timedelta(hours=9))

    def test_split_qualified(self):
        self.assertEqual(split_qualified("google:primary/abc"), ("google", "primary/abc"))
        with self.assertRaises(CalendarError):
            split_qualified("abc")


class RegistryTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(CalendarService.load(path="/nonexistent/calendar.json", instance="tony"))

    def test_unknown_adapter_fails_loud(self):
        path = _write_registry(_registry({
            "weird": {"adapter": "carrier-pigeon", "instances": ["tony"], "status": "active"},
        }))
        try:
            with self.assertRaises(CalendarError):
                CalendarService.load(path=path, instance="tony")
        finally:
            Path(path).unlink()

    def test_instance_and_status_filtering(self):
        path = _write_registry(_registry({
            "google": {"adapter": "google", "instances": ["michael"], "status": "active"},
            "draft": {"adapter": "google", "instances": ["tony"], "status": "planned"},
        }))
        try:
            # tony sees no active providers -> None (disabled), not a crash
            self.assertIsNone(CalendarService.load(path=path, instance="tony"))
        finally:
            Path(path).unlink()

    def test_google_provider_builds(self):
        path = _write_registry(_registry({
            "google": {"adapter": "google", "instances": ["tony"], "status": "active",
                       "token_file": "/tmp/tok.json", "calendars": ["primary"]},
        }))
        try:
            svc = CalendarService.load(path=path, instance="tony")
            self.assertIsNotNone(svc)
            self.assertIn("google", svc.providers)
            self.assertEqual(svc.write, "google")
        finally:
            Path(path).unlink()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregate_merges_and_qualifies(self):
        a = FakeProvider("google", events=[_ev("standup", "2026-09-22T09:00:00+07:00")])
        b = FakeProvider("icloud", events=[_ev("lunch", "2026-09-22T12:00:00+07:00", cal="home")])
        svc = CalendarService({"google": a, "icloud": b}, tz=TZ)
        out = await svc.list_events(day="2026-09-22")
        self.assertEqual([e["title"] for e in out["events"]], ["standup", "lunch"])
        self.assertEqual(out["events"][0]["id"], "google:primary/standup")
        self.assertEqual(out["events"][1]["id"], "icloud:home/lunch")
        self.assertEqual(out["errors"], [])

    async def test_aggregate_isolates_provider_errors(self):
        a = FakeProvider("google", events=[_ev("x", "2026-09-22T09:00:00+07:00")])
        b = FakeProvider("icloud", fail=True)
        svc = CalendarService({"google": a, "icloud": b}, tz=TZ)
        out = await svc.list_events(day="2026-09-22")
        self.assertEqual(len(out["events"]), 1)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("icloud", out["errors"][0])

    async def test_read_pinning(self):
        a = FakeProvider("google", events=[_ev("g", "2026-09-22T09:00:00+07:00")])
        b = FakeProvider("icloud", events=[_ev("i", "2026-09-22T10:00:00+07:00")])
        svc = CalendarService({"google": a, "icloud": b}, read="google", tz=TZ)
        out = await svc.list_events(day="2026-09-22")
        self.assertEqual([e["title"] for e in out["events"]], ["g"])

    async def test_calendar_arg_routes_to_provider(self):
        a = FakeProvider("google")
        b = FakeProvider("icloud", events=[_ev("i", "2026-09-22T10:00:00+07:00", cal="home")])
        svc = CalendarService({"google": a, "icloud": b}, tz=TZ)
        out = await svc.list_events(day="2026-09-22", calendar="icloud:home")
        self.assertEqual([e["title"] for e in out["events"]], ["i"])

    async def test_create_routes_to_write_provider(self):
        a = FakeProvider("google")
        b = FakeProvider("icloud")
        svc = CalendarService({"google": a, "icloud": b}, write="google", tz=TZ)
        ev = await svc.create_event("dentist", "2026-09-23T14:00:00", "2026-09-23T15:00:00")
        self.assertEqual(ev["id"], "google:primary/new1")
        self.assertEqual(len(a.created), 1)
        self.assertEqual(len(b.created), 0)

    async def test_create_explicit_calendar_overrides_write(self):
        a = FakeProvider("google")
        b = FakeProvider("icloud")
        svc = CalendarService({"google": a, "icloud": b}, write="google", tz=TZ)
        await svc.create_event("x", "2026-09-23T14:00:00", "2026-09-23T15:00:00",
                               calendar="icloud:home")
        self.assertEqual(b.created[0][0], "home")
        self.assertEqual(len(a.created), 0)

    async def test_delete_routes_by_qualified_id(self):
        a = FakeProvider("google")
        b = FakeProvider("icloud")
        svc = CalendarService({"google": a, "icloud": b}, tz=TZ)
        await svc.delete_event("icloud:home/abc")
        self.assertEqual(b.deleted, ["home/abc"])
        self.assertEqual(a.deleted, [])

    async def test_plan_day_merges_events_and_tasks(self):
        a = FakeProvider(
            "google",
            events=[_ev("standup", "2026-09-22T09:00:00+07:00")],
            tasks=[Task(id="@default/t1", title="buy milk", provider="google")],
        )
        svc = CalendarService({"google": a}, tz=TZ)
        plan = await svc.plan_day("2026-09-22")
        self.assertEqual(plan["date"], "2026-09-22")
        self.assertEqual(len(plan["events"]), 1)
        self.assertEqual(plan["open_tasks"][0]["title"], "buy milk")
        self.assertEqual(plan["open_tasks"][0]["id"], "google:@default/t1")

    async def test_complete_task_routes(self):
        a = FakeProvider("google")
        svc = CalendarService({"google": a}, tz=TZ)
        await svc.complete_task("google:@default/t9")
        self.assertEqual(a.completed, ["@default/t9"])


def _resp(status=200, payload=None):
    return httpx.Response(status, json=payload if payload is not None else {})


class GoogleAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, client):
        p = GoogleCalendarProvider(
            name="google", token_file="/tmp/nonexistent-token.json",
            calendars=["primary"], client=client,
        )
        return p

    async def test_token_refresh_caches(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _resp(200, {"access_token": "tok1", "expires_in": 3600})
        client.request.return_value = _resp(200, {"items": []})
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        await p.list_events(datetime(2026, 9, 22, tzinfo=TZ), datetime(2026, 9, 23, tzinfo=TZ))
        await p.list_events(datetime(2026, 9, 22, tzinfo=TZ), datetime(2026, 9, 23, tzinfo=TZ))
        self.assertEqual(client.post.call_count, 1)  # refreshed once, reused
        self.assertEqual(client.request.call_count, 2)

    async def test_list_events_normalizes_timed_and_allday(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _resp(200, {"access_token": "t", "expires_in": 3600})
        client.request.return_value = _resp(200, {"items": [
            {"id": "e1", "summary": "standup",
             "start": {"dateTime": "2026-09-22T09:00:00+07:00"},
             "end": {"dateTime": "2026-09-22T09:30:00+07:00"}},
            {"id": "e2", "summary": "holiday",
             "start": {"date": "2026-09-22"}, "end": {"date": "2026-09-23"}},
        ]})
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        events = await p.list_events(datetime(2026, 9, 22, tzinfo=TZ), datetime(2026, 9, 23, tzinfo=TZ))
        self.assertEqual(events[0].id, "primary/e1")
        self.assertFalse(events[0].all_day)
        self.assertTrue(events[1].all_day)
        params = client.request.call_args.kwargs["params"]
        self.assertEqual(params["singleEvents"], "true")
        self.assertIn("timeMin", params)

    async def test_create_event_payload(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = [
            _resp(200, {"access_token": "t", "expires_in": 3600}),
        ]
        client.request.return_value = _resp(200, {
            "id": "new1", "summary": "dentist",
            "start": {"dateTime": "2026-09-23T14:00:00+07:00"},
            "end": {"dateTime": "2026-09-23T15:00:00+07:00"},
        })
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        ev = await p.create_event(
            None, "dentist",
            datetime(2026, 9, 23, 14, tzinfo=TZ), datetime(2026, 9, 23, 15, tzinfo=TZ),
        )
        self.assertEqual(ev.id, "primary/new1")
        body = client.request.call_args.kwargs["json"]
        self.assertEqual(body["summary"], "dentist")
        self.assertIn("dateTime", body["start"])

    async def test_401_raises_auth_error(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _resp(401, {"error": "invalid_grant"})
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        from backend.calendar_providers import CalendarAuthError
        with self.assertRaises(CalendarAuthError):
            await p.list_events(datetime(2026, 9, 22, tzinfo=TZ), datetime(2026, 9, 23, tzinfo=TZ))

    async def test_delete_splits_composite_id(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _resp(200, {"access_token": "t", "expires_in": 3600})
        client.request.return_value = _resp(204)
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        await p.delete_event("primary/abc123")
        args = client.request.call_args
        self.assertEqual(args.args[0], "DELETE")
        self.assertIn("/calendars/primary/events/abc123", args.args[1])

    async def test_tasks_list_and_complete(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _resp(200, {"access_token": "t", "expires_in": 3600})
        client.request.return_value = _resp(200, {"items": [
            {"id": "t1", "title": "buy milk", "status": "needsAction"},
        ]})
        p = self._provider(client)
        p._creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        tasks = await p.list_tasks()
        self.assertEqual(tasks[0].id, "@default/t1")
        url = client.request.call_args.args[1]
        self.assertIn("tasks.googleapis.com", url)


class CalendarGateTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self):
        ha = AsyncMock()
        ha.base_url = "http://test:8123"
        runner = ToolRunner(ha, instance_id="test")
        return runner

    async def test_write_tools_require_confirmed(self):
        runner = self._runner()
        runner._calendar = CalendarService({"fake": FakeProvider("fake")}, tz=TZ)
        runner._calendar_loaded = True
        for tool, args in [
            ("calendar_create_event", {"title": "x", "start": "2026-09-23T14:00", "end": "2026-09-23T15:00"}),
            ("calendar_delete_event", {"event_id": "fake:primary/abc"}),
            ("tasks_add", {"title": "x"}),
            ("tasks_complete", {"task_id": "fake:@default/t1"}),
        ]:
            with self.assertRaises(PermissionError, msg=tool):
                await runner.execute(tool, dict(args))

    async def test_write_tools_pass_with_confirmed(self):
        runner = self._runner()
        provider = FakeProvider("fake")
        runner._calendar = CalendarService({"fake": provider}, write="fake", tz=TZ)
        runner._calendar_loaded = True
        out = await runner.execute("tasks_add", {"title": "buy milk", "confirmed": True})
        self.assertEqual(out["title"], "buy milk")

    async def test_unconfigured_calendar_raises_not_configured(self):
        runner = self._runner()
        runner._calendar = None
        runner._calendar_loaded = True
        with self.assertRaises(RuntimeError):
            await runner.execute("calendar_list_events", {})

    async def test_read_tools_ungated(self):
        runner = self._runner()
        runner._calendar = CalendarService({"fake": FakeProvider("fake")}, tz=TZ)
        runner._calendar_loaded = True
        out = await runner.execute("calendar_list_calendars", {})
        self.assertIn("calendars", out)
        out = await runner.execute("plan_day", {"day": "2026-09-22"})
        self.assertIn("events", out)


if __name__ == "__main__":
    unittest.main()
