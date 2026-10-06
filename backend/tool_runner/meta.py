"""Meta/ops umbrella: usage + summary rollups, ada_ops.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class MetaMixin:

    # -- Token usage reporting (usage_tracker.py) --

    async def ada_usage_summary(self, source: str = "all", reset: bool = False) -> dict[str, Any]:
        report = usage_ledger.snapshot(
            None if str(source).lower() in ("", "all") else str(source).lower()
        )
        if reset:
            usage_ledger.reset()
            report["reset"] = True
        return report

    # -- Summary rollup tools (backend/summary_rollups.py): daily digests
    #    and a weekly comparison over the per-session summaries in the
    #    recall-summary collection. Read-only against user-facing state —
    #    rollup writes land in the internal summary collection like the
    #    session summaries themselves, so no confirmed= gate.

    async def ada_daily_summary(self, day: str = "today", refresh: bool = False) -> dict[str, Any]:
        """Digest of all sessions on one day ('today'|'yesterday'|YYYY-MM-DD)."""
        return await summary_rollups.daily_summary(
            self.mddb, day=str(day), refresh=bool(refresh))

    async def ada_weekly_comparison(
        self, end: str = "today", days: int = 7, refresh: bool = False,
    ) -> dict[str, Any]:
        """Compare the daily digests of the last `days` days — weekly trends."""
        return await summary_rollups.weekly_comparison(
            self.mddb, end=str(end), days=int(days), refresh=bool(refresh))

    async def ada_outcome(
        self,
        bank: str,
        key: str,
        outcome: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record how a remembered fact/check turned out (good/bad/…)."""
        return await memory_ops.record_outcome(
            self.mddb, self.banks, bank, key, outcome, note,
            session_id=self.session_id,
            person_entity=self._memory_identity(),
        )

    # -- Meta/ops umbrella (tools-merge-meta-voice, 2026-10-05): ada_outcome,
    #    ada_usage_summary, ada_mddb_health, ada_decision_check and
    #    ada_deep_research consolidated behind action=. outcome/usage/health
    #    run here; check/research dispatch provider-side on live sessions
    #    (their results arrive as injected text turns) and get an honest
    #    error on this path — pre-merge they were provider-only too.

    async def ada_ops(
        self,
        action: str,
        bank: str | None = None,
        key: str | None = None,
        outcome: str | None = None,
        note: str | None = None,
        source: str = "all",
        reset: bool = False,
        collection: str | None = None,
        product: str | None = None,
        url: str | None = None,
        mode: str | None = None,
        topic: str | None = None,
        depth: str | None = None,
    ) -> dict[str, Any]:
        """Meta/ops tools behind one action= param (the absorbed
        ada_outcome / ada_usage_summary / ada_mddb_health /
        ada_decision_check / ada_deep_research seats)."""
        action = str(action or "").strip().lower()
        if action == "outcome":
            if not bank or not key or not outcome:
                raise ValueError(
                    "action 'outcome' requires bank, key, and outcome")
            return await self.ada_outcome(
                bank=str(bank), key=str(key), outcome=str(outcome),
                note=note)
        if action == "usage":
            return await self.ada_usage_summary(
                source=str(source or "all"), reset=bool(reset))
        if action == "health":
            return await self._ops_mddb_health(collection)
        if action in ("check", "research"):
            return {"error": (
                f"ada_ops action='{action}' only runs inside a live voice "
                "session — its result is spoken back mid-conversation; "
                "it is not available on this call path")}
        raise ValueError(
            f"invalid ada_ops action {action!r}: expected "
            "outcome|usage|health|check|research")

    async def _ops_mddb_health(self, collection: str | None) -> dict[str, Any]:
        """The absorbed ada_mddb_health drop-in (tools.d): MDDB vector-stats
        report — total docs, missing vectors, per-collection lag."""
        if self.mddb is None:
            return {"ok": False, "error": "memory database is not configured"}
        collection = (collection or "").strip()
        resp = await self.mddb._client.get(f"{self.mddb.base_url}/vector-stats")
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
            "collections": (len(stats) if not collection
                            else (1 if collection in stats else 0)),
            "total_documents": total_docs,
            "missing_vectors": total_missing,
        }
        if collection and collection not in stats:
            return {"ok": False, "error": f"no collection named {collection!r}"}
        if rows:
            summary["lagging"] = rows[:8]
            if len(rows) > 8:
                summary["lagging_truncated"] = len(rows) - 8
        else:
            summary["note"] = "all collections fully embedded"
        return summary
