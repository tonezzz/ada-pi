"""Daily and weekly rollup summaries — the tier above per-session reports.

conversation_memory writes one ``session-summary`` doc per voice session
into the recall-summary collection. This module rolls those up:

- ``daily-<date>``   (kind=daily-summary)  — digest of every session that
  day; regenerated after each persisted session and on demand.
- ``weekly-<date>``  (kind=weekly-summary) — comparison across the daily
  digests of a rolling window (default 7 days), for weekly-trend asks.

Both live in the same collection so the mid-tier recall search
(``_recall_summaries`` — "session/daily/weekly/monthly summaries") picks
them up automatically next to session summaries.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

from backend import conversation_memory as cm
from backend.calendar_providers import parse_day

logger = logging.getLogger("tools")

_DAILY_ROLLUP = os.environ.get("ADA_DAILY_ROLLUP", "1").lower() not in (
    "0", "false", "no")
_WEEKLY_MAX_DAYS = 14


def _doc_text(doc: dict[str, Any] | None) -> str:
    if not doc:
        return ""
    return str(doc.get("contentMd") or doc.get("content_md") or "").strip()


async def _llm(prompt: str) -> str | None:
    """One text rollup call — same Gemini REST path as session reports."""
    if not cm._GEMINI_API_KEY:
        cm._report_failure(
            "rollup", "GEMINI_API_KEY not set — summary rollups disabled")
        return None
    try:
        from google import genai
        resp = await genai.Client(api_key=cm._GEMINI_API_KEY).aio.models.generate_content(
            model=os.environ.get("ADA_ROLLUP_MODEL") or cm._REPORT_MODEL,
            contents=prompt,
            config={"temperature": 0.2},
        )
        return (resp.text or "").strip() or None
    except Exception as exc:
        cm._report_failure("rollup", exc)
        return None


async def _session_docs(mddb: Any, day: date) -> list[dict[str, Any]]:
    """Per-session summary docs for one UTC date, oldest key first."""
    docs = await mddb.search_documents(
        collection=cm._summary_collection(),
        filter_meta={"kind": ["session-summary"], "date": [day.isoformat()]},
        limit=50,
    )
    return sorted(docs or [], key=lambda d: str(d.get("key") or ""))


async def _save(mddb: Any, key: str, text: str, kind: str,
                meta: dict[str, list[str]]) -> None:
    meta = {"kind": [kind], "source": ["summary_rollups"], **meta}
    try:
        await mddb.add_document(
            collection=cm._summary_collection(), key=key,
            lang="en", content_md=text, meta=meta)
    except Exception as exc:
        cm._report_failure("rollup_mddb", exc)


async def daily_summary(
    mddb: Any, day: str = "today", refresh: bool = False,
) -> dict[str, Any]:
    """Digest of all sessions on one day ('today'|'yesterday'|YYYY-MM-DD).

    Returns the stored daily doc unless missing or refresh=True, in which
    case the day's session summaries are rolled up through the LLM and
    persisted for later recall.
    """
    target = parse_day(str(day))
    key = f"daily-{target.isoformat()}"
    if mddb is None:
        return {"status": "unavailable", "date": target.isoformat(),
                "error": "summary rollups require MDDB"}
    if not refresh:
        text = _doc_text(await mddb.get_document(cm._summary_collection(), key))
        if text:
            return {"status": "ok", "date": target.isoformat(), "key": key,
                    "summary": text, "generated": False}
    sessions = await _session_docs(mddb, target)
    if not sessions:
        return {"status": "no_sessions", "date": target.isoformat(),
                "summary": None, "sessions": 0}
    lines = [
        f"Session summaries for {target.isoformat()} "
        f"({len(sessions)} session(s)):\n"
    ]
    for doc in sessions:
        body = _doc_text(doc)
        if body:
            lines.append(f"- {body}")
    text = await _llm(
        "You are summarizing one day of Ada voice-assistant sessions for the "
        "user.\n\n" + "\n".join(lines) + "\n\n"
        "Write a daily digest in 3-6 short lines: the main topics, what was "
        "done or decided, requests made, and anything left open. Be concise "
        "and factual; attribute claims ('the user said/asked…') rather than "
        "asserting them. Write in English."
    )
    if not text:
        return {"status": "error", "date": target.isoformat(),
                "error": "summary model unavailable", "sessions": len(sessions)}
    await _save(mddb, key, text, "daily-summary",
                {"date": [target.isoformat()],
                 "sessions": [str(len(sessions))]})
    return {"status": "ok", "date": target.isoformat(), "key": key,
            "sessions": len(sessions), "summary": text, "generated": True}


async def weekly_comparison(
    mddb: Any, end: str = "today", days: int = 7, refresh: bool = False,
) -> dict[str, Any]:
    """Compare the daily digests of a rolling window (weekly trends).

    Ensures a daily doc exists for every in-window day that has sessions,
    then asks the LLM to compare them — recurring themes, day-to-day
    changes, and week-over-week trend when a previous weekly doc exists.
    """
    end_date = parse_day(str(end))
    days = max(2, min(int(days or 7), _WEEKLY_MAX_DAYS))
    dates = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    key = f"weekly-{end_date.isoformat()}"
    if mddb is None:
        return {"status": "unavailable",
                "period": f"{dates[0].isoformat()}..{end_date.isoformat()}",
                "error": "summary rollups require MDDB"}
    if not refresh:
        text = _doc_text(await mddb.get_document(cm._summary_collection(), key))
        if text:
            return {"status": "ok",
                    "period": f"{dates[0].isoformat()}..{end_date.isoformat()}",
                    "key": key, "comparison": text, "generated": False}
    window: list[dict[str, Any]] = []
    for d in dates:
        doc = await mddb.get_document(
            cm._summary_collection(), f"daily-{d.isoformat()}")
        stored = _doc_text(doc)
        sessions = 0
        if stored:
            meta_sessions = (doc.get("meta") or {}).get("sessions") or []
            try:
                sessions = int(meta_sessions[0])
            except (IndexError, ValueError):
                sessions = 0
        else:
            res = await daily_summary(mddb, day=d.isoformat())
            stored = str(res.get("summary") or "")
            sessions = int(res.get("sessions") or 0)
        window.append({"date": d.isoformat(), "sessions": sessions,
                       "summary": stored or None})
    covered = [w for w in window if w["summary"]]
    if len(covered) < 2:
        return {"status": "insufficient_data",
                "period": f"{dates[0].isoformat()}..{end_date.isoformat()}",
                "days": window,
                "error": "fewer than two days in the window have session summaries"}
    prev_key = f"weekly-{(end_date - timedelta(days=days)).isoformat()}"
    prev = _doc_text(await mddb.get_document(cm._summary_collection(), prev_key))
    lines = ["Daily digests:"]
    for w in covered:
        lines.append(f"\n## {w['date']}\n{w['summary']}")
    prompt = (
        "You are comparing one week of Ada voice-assistant daily digests "
        "for the user.\n\n" + "\n".join(lines) + "\n\n"
        "Write a weekly comparison in 4-8 short lines or bullets: recurring "
        "themes across days, what changed during the week, notable one-off "
        "events, and open items carried forward. Be concise and factual; "
        "attribute claims rather than asserting them. Write in English."
    )
    if prev:
        prompt += (
            "\n\nThe previous window's comparison was:\n" + prev +
            "\nEnd with one line on the week-over-week trend."
        )
    text = await _llm(prompt)
    if not text:
        return {"status": "error",
                "period": f"{dates[0].isoformat()}..{end_date.isoformat()}",
                "days": window, "error": "summary model unavailable"}
    await _save(mddb, key, text, "weekly-summary",
                {"window_start": [dates[0].isoformat()],
                 "window_end": [end_date.isoformat()],
                 "days_with_data": [str(len(covered))]})
    return {"status": "ok",
            "period": f"{dates[0].isoformat()}..{end_date.isoformat()}",
            "key": key, "days": window, "comparison": text,
            "generated": True}


async def refresh_daily(mddb: Any, day: str = "today") -> None:
    """Regenerate a day's rollup after a new session summary lands.

    Called from conversation_memory._update_summary on session persist;
    failures are recorded in conversation_health and never propagate.
    """
    if not _DAILY_ROLLUP or mddb is None:
        return
    try:
        result = await daily_summary(mddb, day=day, refresh=True)
        if result.get("generated"):
            logger.info(
                "daily rollup %s regenerated over %s session(s)",
                result.get("date"), result.get("sessions"))
    except Exception as exc:
        cm._report_failure("daily_rollup", exc)
