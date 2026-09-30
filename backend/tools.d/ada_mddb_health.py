"""ada_mddb_health — example drop-in tool.

Reports MDDB collection/vector health for the current instance: total docs,
missing vectors, and per-collection embedding lag. Read-only.
"""

from __future__ import annotations

from typing import Any

DECLARATION = {
    "name": "ada_mddb_health",
    "description": (
        "Check the health of the memory database: collections, embedded "
        "documents, and how many documents are still missing vectors. Use "
        "when asked about memory/knowledge-base health or search problems."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "collection": {
                "type": "string",
                "description": "Optional: limit the report to one collection name.",
            },
        },
    },
}

_MAX_LINES = 8


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    collection = (args.get("collection") or "").strip()
    resp = await runner.mddb._client.get(f"{runner.mddb.base_url}/vector-stats")
    resp.raise_for_status()
    stats = resp.json().get("collections") or {}
    rows = []
    total_docs = total_missing = 0
    for name, v in sorted(stats.items()):
        if collection and name != collection:
            continue
        total = int(v.get("total_documents") or 0)
        embedded = int(v.get("embedded_documents") or 0)
        missing = max(0, total - embedded)
        total_docs += total
        total_missing += missing
        if missing:
            rows.append(f"{name}: {missing} missing of {total}")
    summary = {
        "ok": True,
        "collections": len(stats) if not collection else (1 if collection in stats else 0),
        "total_documents": total_docs,
        "missing_vectors": total_missing,
    }
    if collection and collection not in stats:
        return {"ok": False, "error": f"no collection named {collection!r}"}
    if rows:
        summary["lagging"] = rows[:_MAX_LINES]
        if len(rows) > _MAX_LINES:
            summary["lagging_truncated"] = len(rows) - _MAX_LINES
    else:
        summary["note"] = "all collections fully embedded"
    return summary
