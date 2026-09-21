"""Provider-agnostic calendar/tasks backend for Ada.

Each provider adapter (Google Calendar/Tasks, CalDAV, MS Graph) normalizes
into the CalendarEvent/Task models here; ToolRunner methods only ever talk
to CalendarService, so adding a provider is one adapter file plus one
registry block — tool declarations and prompts never change.

Registry JSON is rendered from docs/ssot/apps/ssot.apps.ada-calendar.yml to
~/.config/ada/calendar.json (override: ADA_CALENDAR_FILE):

    {"calendar": {
        "providers": {
            "google": {"adapter": "google", "tasks": true,
                       "instances": ["tony"], "status": "active",
                       "token_file": "~/.config/secrets/ada-google-calendar-token.json",
                       "calendars": ["primary"]}
        },
        "read": "aggregate",    # or a single provider name to pin reads
        "write": "google"       # default write target
    }}

Design rules (decision record: ada-calendar-provider-backend):
  * aggregate reads across active providers; never replicate events between
    providers (sync conflicts and duplicates are not worth it)
  * writes route to the `write` provider unless the caller names
    "provider:calendar_id" explicitly
  * same fail-fast identity rule as memory banks: an active provider with an
    unknown adapter raises at registry load; a missing registry file means
    "calendar not configured" on this instance, not a crash
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from backend.instance import ada_instance_id

logger = logging.getLogger("calendar")

DEFAULT_FILE = "~/.config/ada/calendar.json"
DEFAULT_TZ = "Asia/Bangkok"
PROVIDER_SEP = ":"


class CalendarError(RuntimeError):
    """Base error surfaced to the model as {"error": str}."""


class CalendarAuthError(CalendarError):
    """Provider credentials missing/expired — the model should say so plainly."""


@dataclass
class CalendarEvent:
    id: str            # provider-qualified "provider:calid/eid" once routed
    title: str
    start: str         # ISO 8601 (dateTime, or date for all-day)
    end: str
    provider: str
    calendar: str
    all_day: bool = False
    notes: str | None = None
    location: str | None = None


@dataclass
class Task:
    id: str            # provider-qualified "provider:listid/taskid"
    title: str
    provider: str
    task_list: str | None = None
    due: str | None = None
    notes: str | None = None
    status: str = "needsAction"  # needsAction | completed


@runtime_checkable
class CalendarProvider(Protocol):
    name: str

    async def list_calendars(self) -> list[dict[str, str]]: ...
    async def list_events(
        self, start: datetime, end: datetime, query: str | None = None,
    ) -> list[CalendarEvent]: ...
    async def create_event(
        self, calendar: str | None, title: str, start: datetime | date,
        end: datetime | date, notes: str | None = None, location: str | None = None,
    ) -> CalendarEvent: ...
    async def delete_event(self, raw_id: str) -> None: ...
    async def freebusy(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


@runtime_checkable
class TasksProvider(Protocol):
    name: str

    async def list_task_lists(self) -> list[dict[str, str]]: ...
    async def list_tasks(
        self, task_list: str | None = None, due_before: datetime | None = None,
    ) -> list[Task]: ...
    async def add_task(
        self, title: str, due: date | None = None, notes: str | None = None,
        task_list: str | None = None,
    ) -> Task: ...
    async def complete_task(self, raw_id: str) -> None: ...


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("ADA_TIMEZONE", DEFAULT_TZ))


def parse_day(text: str | None, tz: ZoneInfo | None = None) -> date:
    """'today'|'tomorrow'|'yesterday'|YYYY-MM-DD -> date in local tz."""
    tz = tz or local_tz()
    t = str(text or "").strip().lower()
    today = datetime.now(tz).date()
    if t in ("", "today"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(t)
    except ValueError as exc:
        raise CalendarError(
            f"unparseable date {text!r}; use today|tomorrow|YYYY-MM-DD"
        ) from exc


def day_range(day: date, days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    days = max(1, min(int(days or 1), 31))
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=days)


def _qualify(provider: str, raw_id: str) -> str:
    return raw_id if raw_id.startswith(f"{provider}{PROVIDER_SEP}") else f"{provider}{PROVIDER_SEP}{raw_id}"


def split_qualified(qid: str) -> tuple[str, str]:
    provider, sep, raw = str(qid).partition(PROVIDER_SEP)
    if not sep or not provider or not raw:
        raise CalendarError(
            f"id {qid!r} must be provider-qualified (e.g. 'google:primary/abc')"
        )
    return provider, raw


def _parse_when(value: str, tz: ZoneInfo) -> datetime | date:
    """ISO datetime or bare date; naive datetimes get local tz."""
    v = str(value).strip()
    try:
        return date.fromisoformat(v)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as exc:
        raise CalendarError(f"unparseable date/time {value!r}; use ISO 8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


class CalendarService:
    """Aggregating facade over the providers configured for this instance."""

    def __init__(
        self,
        providers: dict[str, Any],
        read: str = "aggregate",
        write: str | None = None,
        tz: ZoneInfo | None = None,
    ) -> None:
        self.providers = providers
        self.read = read or "aggregate"
        self.write = write or next(iter(providers), None)
        self.tz = tz or local_tz()

    # -- registry ----------------------------------------------------------

    @classmethod
    def load(
        cls, path: str | None = None, instance: str | None = None,
    ) -> "CalendarService | None":
        """Load the rendered registry. None = calendar not configured here."""
        path = path or os.environ.get("ADA_CALENDAR_FILE", DEFAULT_FILE)
        p = Path(os.path.expanduser(path))
        if not p.exists():
            logger.info("calendar registry %s absent — calendar tools disabled", p)
            return None
        try:
            data = json.loads(p.read_text())
        except ValueError as exc:
            raise CalendarError(f"calendar registry {p} is not valid JSON: {exc}") from exc
        spec = data.get("calendar", data)
        providers_spec = spec.get("providers") or {}
        instance = instance or ada_instance_id()

        providers: dict[str, Any] = {}
        for name, cfg in providers_spec.items():
            if not isinstance(cfg, dict):
                raise CalendarError(f"calendar provider {name!r} is not a mapping")
            if cfg.get("status", "active") != "active":
                continue
            if instance not in (cfg.get("instances") or []):
                continue
            providers[name] = cls._build(name, cfg)
        if not providers:
            logger.info("calendar registry %s: no active providers for instance=%s", p, instance)
            return None
        return cls(providers, read=spec.get("read", "aggregate"), write=spec.get("write"))

    @staticmethod
    def _build(name: str, cfg: dict[str, Any]) -> Any:
        adapter = cfg.get("adapter")
        if adapter == "google":
            from backend.google_calendar import GoogleCalendarProvider

            return GoogleCalendarProvider(
                name=name,
                token_file=cfg.get("token_file"),
                calendars=cfg.get("calendars"),
                default_calendar=cfg.get("default_calendar"),
                task_list=cfg.get("task_list"),
                has_tasks=bool(cfg.get("tasks", True)),
            )
        raise CalendarError(
            f"calendar provider {name!r} uses unknown adapter {adapter!r}; "
            "register the adapter in CalendarService._build"
        )

    # -- reads ---------------------------------------------------------------

    def _read_providers(self) -> list[Any]:
        if self.read != "aggregate":
            provider = self.providers.get(self.read)
            if provider is None:
                raise CalendarError(f"read provider {self.read!r} is not active on this instance")
            return [provider]
        return list(self.providers.values())

    def _provider(self, name: str) -> Any:
        provider = self.providers.get(name)
        if provider is None:
            raise CalendarError(
                f"provider {name!r} is not active on this instance "
                f"(have: {', '.join(self.providers)})"
            )
        return provider

    async def list_calendars(self) -> dict[str, Any]:
        calendars: list[dict[str, Any]] = []
        errors: list[str] = []
        results = await asyncio.gather(
            *(p.list_calendars() for p in self.providers.values()),
            return_exceptions=True,
        )
        for name, res in zip(self.providers, results):
            if isinstance(res, Exception):
                errors.append(f"{name}: {res}")
            else:
                for cal in res:
                    cal["provider"] = name
                    cal["qualified"] = f"{name}{PROVIDER_SEP}{cal['id']}"
                    calendars.append(cal)
        return {"calendars": calendars, "default_write": self.write, "errors": errors}

    async def list_events(
        self,
        day: str | None = "today",
        days: int = 1,
        query: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        start, end = day_range(parse_day(day, self.tz), days, self.tz)
        if calendar and PROVIDER_SEP in calendar:
            pname, calid = calendar.split(PROVIDER_SEP, 1)
            targets = [(self._provider(pname), calid)]
        elif self.read != "aggregate":
            targets = [(self._provider(self.read), calendar)]
        else:
            targets = [(p, calendar) for p in self.providers.values()]

        events: list[dict[str, Any]] = []
        errors: list[str] = []
        results = await asyncio.gather(
            *(p.list_events(start, end, query=query) for p, _ in targets),
            return_exceptions=True,
        )
        for (p, cal), res in zip(targets, results):
            if isinstance(res, Exception):
                errors.append(f"{p.name}: {res}")
                continue
            for ev in res:
                if cal is not None and ev.calendar != cal:
                    continue
                ev.id = _qualify(p.name, ev.id)
                events.append(asdict(ev))
        events.sort(key=lambda e: (e["start"], e["title"]))
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": str(self.tz),
            "events": events,
            "errors": errors,
        }

    async def freebusy(self, day: str | None = "today", days: int = 1) -> dict[str, Any]:
        start, end = day_range(parse_day(day, self.tz), days, self.tz)
        providers = self._read_providers()
        busy: list[dict[str, Any]] = []
        errors: list[str] = []
        results = await asyncio.gather(
            *(p.freebusy(start, end) for p in providers),
            return_exceptions=True,
        )
        for p, res in zip(providers, results):
            if isinstance(res, Exception):
                errors.append(f"{p.name}: {res}")
            else:
                for item in res:
                    item["provider"] = p.name
                    busy.append(item)
        return {"start": start.isoformat(), "end": end.isoformat(), "busy": busy, "errors": errors}

    async def plan_day(self, day: str | None = "today") -> dict[str, Any]:
        events, tasks = await asyncio.gather(
            self.list_events(day=day, days=1),
            self.list_tasks(),
        )
        return {
            "date": parse_day(day, self.tz).isoformat(),
            "timezone": str(self.tz),
            "events": events["events"],
            "open_tasks": tasks["tasks"],
            "errors": events["errors"] + tasks["errors"],
        }

    # -- writes ---------------------------------------------------------------

    def _write_provider(self, calendar: str | None) -> tuple[Any, str | None]:
        if calendar and PROVIDER_SEP in calendar:
            pname, calid = calendar.split(PROVIDER_SEP, 1)
            return self._provider(pname), calid
        if self.write is None:
            raise CalendarError("no write provider configured")
        return self._provider(self.write), calendar

    async def create_event(
        self,
        title: str,
        start: str,
        end: str,
        calendar: str | None = None,
        notes: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        provider, calid = self._write_provider(calendar)
        ev = await provider.create_event(
            calid, title, _parse_when(start, self.tz), _parse_when(end, self.tz),
            notes=notes, location=location,
        )
        ev.id = _qualify(provider.name, ev.id)
        return asdict(ev)

    async def delete_event(self, event_id: str) -> str:
        pname, raw = split_qualified(event_id)
        await self._provider(pname).delete_event(raw)
        return f"deleted {event_id}"

    # -- tasks ----------------------------------------------------------------

    def _task_providers(self) -> list[Any]:
        return [p for p in self.providers.values() if isinstance(p, TasksProvider)]

    async def list_task_lists(self) -> dict[str, Any]:
        lists: list[dict[str, Any]] = []
        errors: list[str] = []
        providers = self._task_providers()
        results = await asyncio.gather(
            *(p.list_task_lists() for p in providers),
            return_exceptions=True,
        )
        for p, res in zip(providers, results):
            if isinstance(res, Exception):
                errors.append(f"{p.name}: {res}")
            else:
                for tl in res:
                    tl["provider"] = p.name
                    lists.append(tl)
        return {"task_lists": lists, "errors": errors}

    async def list_tasks(self, task_list: str | None = None) -> dict[str, Any]:
        providers = self._task_providers()
        tasks: list[dict[str, Any]] = []
        errors: list[str] = []
        results = await asyncio.gather(
            *(p.list_tasks(task_list=task_list) for p in providers),
            return_exceptions=True,
        )
        for p, res in zip(providers, results):
            if isinstance(res, Exception):
                errors.append(f"{p.name}: {res}")
                continue
            for t in res:
                t.id = _qualify(p.name, t.id)
                tasks.append(asdict(t))
        tasks.sort(key=lambda t: (t.get("due") or "9999", t["title"]))
        return {"tasks": tasks, "errors": errors}

    async def add_task(
        self, title: str, due: str | None = None, notes: str | None = None,
        task_list: str | None = None,
    ) -> dict[str, Any]:
        provider: Any = None
        if task_list and PROVIDER_SEP in task_list:
            pname, task_list = task_list.split(PROVIDER_SEP, 1)
            provider = self._provider(pname)
        elif self.write:
            provider = self._provider(self.write)
        if provider is None:
            raise CalendarError("no write provider configured")
        if not isinstance(provider, TasksProvider):
            raise CalendarError(f"provider {provider.name!r} does not support tasks")
        due_date = parse_day(due, self.tz) if due else None
        task = await provider.add_task(title, due=due_date, notes=notes, task_list=task_list)
        task.id = _qualify(provider.name, task.id)
        return asdict(task)

    async def complete_task(self, task_id: str) -> str:
        pname, raw = split_qualified(task_id)
        provider = self._provider(pname)
        if not isinstance(provider, TasksProvider):
            raise CalendarError(f"provider {pname!r} does not support tasks")
        await provider.complete_task(raw)
        return f"completed {task_id}"
