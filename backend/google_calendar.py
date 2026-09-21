"""Google Calendar + Google Tasks adapter for Ada's calendar backend.

httpx-native (no google-api-python-client): the only auth we need is the
offline refresh-token exchange — one POST to the token endpoint, cached in
memory until expiry. Token file is minted by
scripts/ada/google-calendar-auth.py and holds:

    {"client_id": ..., "client_secret": ..., "refresh_token": ...,
     "token_uri": "https://oauth2.googleapis.com/token"}   # token_uri optional

Default token path: ~/.config/secrets/ada-google-calendar-token.json
(override: ADA_GOOGLE_TOKEN_FILE, or per-provider `token_file` in the
registry). The OAuth *client* is shared with the Drive backup —
~/.config/secrets/google_credentials.json on project gen-lang-client-* —
but the refresh token here is ada-specific (scopes: calendar.readonly,
calendar.events, tasks).

Raw ids are "calid/eventid" and "listid/taskid"; CalendarService qualifies
them as "google:<raw>" before they reach the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from backend.calendar_providers import (
    CalendarAuthError,
    CalendarError,
    CalendarEvent,
    Task,
)

logger = logging.getLogger("calendar.google")

CAL_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_TOKEN_FILE = "~/.config/secrets/ada-google-calendar-token.json"
DEFAULT_TASK_LIST = "@default"


def _q(value: str) -> str:
    """URL-encode a calendar/event/task id for use as a path segment —
    Google calendar ids can contain '#' (e.g. en.th#holiday@group...)."""
    return urllib.parse.quote(str(value), safe="")


class GoogleCalendarProvider:
    """CalendarProvider + TasksProvider over the Google REST APIs."""

    def __init__(
        self,
        name: str = "google",
        token_file: str | None = None,
        calendars: list[str] | None = None,
        default_calendar: str | None = None,
        task_list: str | None = None,
        has_tasks: bool = True,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.token_file = os.path.expanduser(
            token_file or os.environ.get("ADA_GOOGLE_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        )
        self.calendars = list(calendars) if calendars else ["primary"]
        self.default_calendar = default_calendar or self.calendars[0]
        self.task_list = task_list or DEFAULT_TASK_LIST
        self.has_tasks = has_tasks
        self.timeout = timeout
        self._client = client
        self._creds: dict[str, Any] | None = None
        self._access_token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()

    # -- auth ----------------------------------------------------------------

    def _load_creds(self) -> dict[str, Any]:
        if self._creds is not None:
            return self._creds
        p = Path(self.token_file)
        try:
            data = json.loads(p.read_text())
        except OSError as exc:
            raise CalendarAuthError(
                f"{self.name}: token file {p} unreadable — run "
                "scripts/ada/google-calendar-auth.py"
            ) from exc
        except ValueError as exc:
            raise CalendarAuthError(f"{self.name}: token file {p} is not valid JSON") from exc
        for field in ("client_id", "client_secret", "refresh_token"):
            if not data.get(field):
                raise CalendarAuthError(f"{self.name}: token file {p} missing {field!r}")
        self._creds = data
        return data

    async def _access(self) -> str:
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_exp:
                return self._access_token
            creds = self._load_creds()
            resp = await self._http().post(
                creds.get("token_uri") or TOKEN_URI,
                data={
                    "grant_type": "refresh_token",
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "refresh_token": creds["refresh_token"],
                },
            )
            if resp.status_code != 200:
                raise CalendarAuthError(
                    f"{self.name}: token refresh failed ({resp.status_code}) — "
                    "re-run scripts/ada/google-calendar-auth.py"
                )
            body = resp.json()
            self._access_token = body["access_token"]
            self._token_exp = time.monotonic() + int(body.get("expires_in", 3600)) - 60
            return self._access_token

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _req(self, method: str, url: str, **kwargs: Any) -> Any:
        token = await self._access()
        resp = await self._http().request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            raise CalendarAuthError(
                f"{self.name}: access token rejected — re-run "
                "scripts/ada/google-calendar-auth.py"
            )
        if resp.status_code == 404:
            raise CalendarError(f"{self.name}: not found ({url})")
        if resp.status_code >= 400:
            raise CalendarError(f"{self.name}: {method} {url} -> {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- CalendarProvider ------------------------------------------------------

    async def list_calendars(self) -> list[dict[str, str]]:
        data = await self._req("GET", f"{CAL_API}/users/me/calendarList")
        return [
            {
                "id": c["id"],
                "title": c.get("summary") or c["id"],
                "primary": bool(c.get("primary")),
            }
            for c in (data or {}).get("items", [])
        ]

    async def list_events(
        self, start: datetime, end: datetime, query: str | None = None,
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for calid in self.calendars:
            params: dict[str, Any] = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 50,
            }
            if query:
                params["q"] = query
            data = await self._req("GET", f"{CAL_API}/calendars/{_q(calid)}/events", params=params)
            for item in (data or {}).get("items", []):
                events.append(self._norm_event(item, calid))
        return events

    async def create_event(
        self, calendar: str | None, title: str, start: datetime | date,
        end: datetime | date, notes: str | None = None, location: str | None = None,
    ) -> CalendarEvent:
        calid = calendar or self.default_calendar
        body: dict[str, Any] = {
            "summary": title,
            "start": _when_payload(start),
            "end": _when_payload(end),
        }
        if notes:
            body["description"] = notes
        if location:
            body["location"] = location
        data = await self._req("POST", f"{CAL_API}/calendars/{_q(calid)}/events", json=body)
        return self._norm_event(data, calid)

    async def delete_event(self, raw_id: str) -> None:
        calid, _, event_id = raw_id.partition("/")
        if not event_id:
            raise CalendarError(f"{self.name}: malformed event id {raw_id!r}")
        await self._req("DELETE", f"{CAL_API}/calendars/{_q(calid)}/events/{_q(event_id)}")

    async def freebusy(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        data = await self._req("POST", f"{CAL_API}/freeBusy", json={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": c} for c in self.calendars],
        })
        out = []
        for calid, info in (data or {}).get("calendars", {}).items():
            for slot in info.get("busy", []):
                out.append({"calendar": calid, "start": slot["start"], "end": slot["end"]})
        return out

    # -- TasksProvider ---------------------------------------------------------

    async def list_task_lists(self) -> list[dict[str, str]]:
        data = await self._req("GET", f"{TASKS_API}/users/@me/lists")
        return [{"id": t["id"], "title": t.get("title") or t["id"]} for t in (data or {}).get("items", [])]

    async def list_tasks(
        self, task_list: str | None = None, due_before: datetime | None = None,
    ) -> list[Task]:
        if not self.has_tasks:
            raise CalendarError(f"{self.name}: tasks disabled in registry")
        lid = task_list or self.task_list
        params: dict[str, Any] = {"showCompleted": "false", "showHidden": "false", "maxResults": 100}
        if due_before is not None:
            params["dueMax"] = due_before.isoformat()
        data = await self._req("GET", f"{TASKS_API}/lists/{_q(lid)}/tasks", params=params)
        return [self._norm_task(t, lid) for t in (data or {}).get("items", [])]

    async def add_task(
        self, title: str, due: date | None = None, notes: str | None = None,
        task_list: str | None = None,
    ) -> Task:
        if not self.has_tasks:
            raise CalendarError(f"{self.name}: tasks disabled in registry")
        lid = task_list or self.task_list
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due is not None:
            # Tasks API due is RFC3339; the time part is ignored (date-only tasks)
            body["due"] = datetime(due.year, due.month, due.day).isoformat() + "Z"
        data = await self._req("POST", f"{TASKS_API}/lists/{_q(lid)}/tasks", json=body)
        return self._norm_task(data, lid)

    async def complete_task(self, raw_id: str) -> None:
        lid, _, task_id = raw_id.partition("/")
        if not task_id:
            raise CalendarError(f"{self.name}: malformed task id {raw_id!r}")
        await self._req(
            "PATCH", f"{TASKS_API}/lists/{_q(lid)}/tasks/{_q(task_id)}",
            json={"status": "completed"},
        )

    # -- normalization ---------------------------------------------------------

    def _norm_event(self, item: dict[str, Any], calid: str) -> CalendarEvent:
        s = item.get("start") or {}
        e = item.get("end") or {}
        all_day = "date" in s
        return CalendarEvent(
            id=f"{calid}/{item.get('id', '')}",
            title=item.get("summary") or "(no title)",
            start=s.get("dateTime") or s.get("date") or "",
            end=e.get("dateTime") or e.get("date") or "",
            provider=self.name,
            calendar=calid,
            all_day=all_day,
            notes=item.get("description"),
            location=item.get("location"),
        )

    def _norm_task(self, item: dict[str, Any], lid: str) -> Task:
        return Task(
            id=f"{lid}/{item.get('id', '')}",
            title=item.get("title") or "(untitled)",
            provider=self.name,
            task_list=lid,
            due=item.get("due"),
            notes=item.get("notes"),
            status=item.get("status", "needsAction"),
        )


def _when_payload(value: datetime | date) -> dict[str, str]:
    if isinstance(value, datetime):
        return {"dateTime": value.isoformat(), "timeZone": str(value.tzinfo or "Asia/Bangkok")}
    return {"date": value.isoformat()}
