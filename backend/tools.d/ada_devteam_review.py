"""ada_devteam_review — drop-in tool.

Ada describes a tool she wants; the devteam pipeline drafts a spec, runs a
parallel expert panel (security/privacy, standards, QA, scope), revises
the spec, and persists the result to the devin-handoff bank where the
dispatch pipeline picks it up. Owner-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DECLARATION = {
    "name": "ada_devteam_review",
    "description": (
        "Design-review a proposed new tool before it is built. Drafts a "
        "tool spec, runs it through the dev-team expert panel (security, "
        "standards, QA, scope), revises it, and files the reviewed spec "
        "into the devin handoff bank. Use when asked to design, spec, or "
        "review a new Ada tool or capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "Plain-language description of the tool Ada wants.",
            },
            "title": {
                "type": "string",
                "description": "Optional short slug for the spec doc key.",
            },
        },
        "required": ["request"],
    },
}


def _slug(text: str, words: int = 5) -> str:
    import re
    parts = re.findall(r"[a-z0-9]+", text.lower())[:words]
    return "-".join(parts) or "untitled"


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    from backend import devteam

    request = (args.get("request") or "").strip()
    if not request:
        return {"ok": False, "error": "request is required"}
    result = await devteam.review(request)

    panel = result["panel"]
    lines = [
        f"# Dev-team review: {(args.get('title') or request)[:80]}",
        "",
        f"- verdict: **{result['verdict']}**",
        f"- model: {result['model']}",
        f"- reviewed: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Panel",
    ]
    lines += [
        f"- **{r['role']}** — {r['verdict']}: {r['summary']}" +
        (f" ({'; '.join(r['comments'])})" if r["comments"] else "")
        for r in panel
    ]
    lines += ["", "## Revised spec", "", result["spec"]]

    now = datetime.now(timezone.utc)
    key = f"spec/{now.strftime('%Y%m%d-%H%M%S')}-{_slug(request)}"
    await runner.mddb.add_document(
        "ada-ha-bank-devin-handoff",
        key=key, lang="en", content_md="\n".join(lines),
        meta={
            "kind": ["spec-review"], "status": [result["verdict"]],
            "ts": [now.isoformat()], "source": ["voice"],
            "written_by": ["ada"], "scope": ["tony"],
            "bank": ["devin-handoff"],
            "subject": [_slug(request, 8)],
        },
    )
    return {
        "ok": True,
        "verdict": result["verdict"],
        "spec_key": key,
        "panel": {r["role"]: r["verdict"] for r in panel},
        "summary": " | ".join(
            f"{r['role']}: {r['summary']}" for r in panel)[:600],
    }
