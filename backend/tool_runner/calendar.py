"""Calendar / tasks tools (provider-agnostic).

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class CalendarMixin:

    # -- Calendar / tasks tools (provider-agnostic; see ssot.apps.ada-calendar.yml) --

    @property
    def calendar(self) -> CalendarService | None:
        """Rendered provider registry, loaded on first use. None = not configured."""
        if not self._calendar_loaded:
            self._calendar_loaded = True
            try:
                self._calendar = CalendarService.load(instance=self._instance_id)
            except Exception as exc:
                logger.warning("calendar registry load failed: %s", exc)
                self._calendar = None
        return self._calendar

    def _calendar_svc(self) -> CalendarService:
        svc = self.calendar
        if svc is None:
            raise RuntimeError(
                "calendar is not configured on this instance — render "
                "ssot.apps.ada-calendar.yml to ~/.config/ada/calendar.json"
            )
        return svc

    async def calendar_read(
        self,
        action: str = "events",
        day: str = "today",
        days: int = 1,
        query: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        """Calendar reads — tools-merge-calendar-plan consolidated
        calendar_list_events / calendar_list_calendars / calendar_freebusy.
        Free reads — no confirmation."""
        action = (action or "").strip().lower()
        if action == "calendars":
            return await self.calendar_list_calendars()
        if action == "events":
            return await self.calendar_list_events(
                day=day, days=days, query=query, calendar=calendar)
        if action == "freebusy":
            return await self.calendar_freebusy(day=day, days=days)
        raise ValueError(
            f"invalid action {action!r}: expected events|calendars|freebusy")

    async def calendar_write(
        self,
        action: str,
        title: str = "",
        start: str = "",
        end: str = "",
        notes: str | None = None,
        location: str | None = None,
        calendar: str | None = None,
        event_id: str = "",
        to: str = "tomorrow",
    ) -> Any:
        """Calendar writes — tools-merge-calendar-plan consolidated
        calendar_create_event / calendar_delete_event /
        calendar_shift_overdue. Every action mutates the real calendar —
        the CALENDAR_WRITE_TOOLS seat keeps confirmed=true mandatory."""
        action = (action or "").strip().lower()
        if action == "create":
            return await self.calendar_create_event(
                title=title, start=start, end=end, notes=notes,
                location=location, calendar=calendar)
        if action == "delete":
            return await self.calendar_delete_event(event_id=event_id)
        if action == "shift":
            return await self.calendar_shift_overdue(to=to)
        raise ValueError(
            f"invalid action {action!r}: expected create|delete|shift")

    async def calendar_list_calendars(self) -> dict[str, Any]:
        return await self._calendar_svc().list_calendars()

    async def calendar_list_events(
        self,
        day: str = "today",
        days: int = 1,
        query: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().list_events(
            day=str(day), days=int(days),
            query=str(query) if query else None,
            calendar=str(calendar) if calendar else None,
        )

    async def calendar_create_event(
        self,
        title: str,
        start: str,
        end: str,
        notes: str | None = None,
        location: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().create_event(
            str(title), str(start), str(end),
            calendar=str(calendar) if calendar else None,
            notes=str(notes) if notes else None,
            location=str(location) if location else None,
        )

    async def calendar_delete_event(self, event_id: str) -> str:
        return await self._calendar_svc().delete_event(str(event_id))

    async def calendar_freebusy(self, day: str = "today", days: int = 1) -> dict[str, Any]:
        return await self._calendar_svc().freebusy(day=str(day), days=int(days))

    async def tasks(
        self,
        action: str = "list",
        title: str = "",
        task_id: str = "",
        due: str | None = None,
        notes: str | None = None,
        task_list: str | None = None,
    ) -> Any:
        """Task list management — tools-merge-tasks-status consolidated
        tasks_list / tasks_add / tasks_complete / tasks_move into one
        action= tool. 'list' is a free read; 'add'/'done'/'move' mutate —
        the CALENDAR_WRITE_TOOLS seat keeps confirmed=true mandatory for
        them (per-action split in _check_calendar_write_allowed)."""
        action = (action or "list").strip().lower()
        if action == "list":
            return await self.tasks_list(task_list)
        if action == "add":
            return await self.tasks_add(
                str(title), due=due, notes=notes, task_list=task_list)
        if action == "done":
            return await self.tasks_complete(str(task_id))
        if action == "move":
            return await self.tasks_move(str(task_id), str(due or ""))
        raise ValueError(
            f"invalid action {action!r}: expected add|list|done|move")

    async def tasks_list(self, task_list: str | None = None) -> dict[str, Any]:
        return await self._calendar_svc().list_tasks(
            task_list=str(task_list) if task_list else None
        )

    async def tasks_add(
        self,
        title: str,
        due: str | None = None,
        notes: str | None = None,
        task_list: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().add_task(
            str(title),
            due=str(due) if due else None,
            notes=str(notes) if notes else None,
            task_list=str(task_list) if task_list else None,
        )

    async def tasks_complete(self, task_id: str) -> str:
        return await self._calendar_svc().complete_task(str(task_id))

    async def tasks_move(self, task_id: str, due: str) -> dict[str, Any]:
        """Reschedule one task's due date — same task, new date."""
        return await self._calendar_svc().move_task(
            str(task_id), str(due))

    async def calendar_shift_overdue(self, to: str = "tomorrow") -> dict[str, Any]:
        """Move every overdue task + already-ended event to a new day.
        Returns per-item old->new so Ada can report exactly what moved."""
        return await self._calendar_svc().shift_overdue(to=str(to))

    async def plan_day(
        self,
        period: str = "today",
        day: str | None = None,
        days: int = 7,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """One 'how does <period> look' entry — tools-merge-calendar-plan
        absorbed ada_daily_summary + ada_weekly_comparison here.

        period='today'|'tomorrow' → that day's merged events+tasks view,
        with the day's session digest folded in under 'digest' (the
        absorbed ada_daily_summary — for a day with no sessions it reports
        no_sessions). day= overrides the target ('yesterday', YYYY-MM-DD)
        and also works without a configured calendar — the digest alone
        is the day view then. period='week' → the weekly digest
        comparison (absorbed ada_weekly_comparison): day= sets the window
        end, days= the window size."""
        period = str(period or "today").strip().lower()
        if period == "week":
            return await self.ada_weekly_comparison(
                end=str(day or "today"), days=int(days or 7),
                refresh=bool(refresh))
        if period == "digest":
            # ada_daily_summary's alias seat — keeps the absorbed tool's
            # bare-digest contract for /api/tools/call consumers.
            return await self.ada_daily_summary(
                day=str(day or "today"), refresh=bool(refresh))
        target = str(day or period or "today")
        svc = self.calendar
        plan = await svc.plan_day(day=target) if svc is not None else None
        digest = await self.ada_daily_summary(
            day=target, refresh=bool(refresh))
        if plan is None:
            digest.setdefault("calendar", "not configured")
            return digest
        plan["digest"] = digest
        return plan

    async def ada_resolve_action(self, key: str, resolution: str) -> str:
        """Resolve a pending action proposal (applied|dismissed). Bookkeeping
        only — not a calendar write, so no confirmed gate."""
        from backend.conversation_memory import resolve_action_proposal
        return await resolve_action_proposal(str(key), str(resolution))
