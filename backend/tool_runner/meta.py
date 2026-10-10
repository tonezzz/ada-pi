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
        node: str | None = None,
        identity: str | None = None,
        entity_id: str | None = None,
        tool: str | None = None,
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
        if action == "report":
            return await self._ops_report(node, depth)
        if action == "acl_explain":
            return self._ops_acl_explain(identity, entity_id, bank, tool)
        if action in ("check", "research"):
            return {"error": (
                f"ada_ops action='{action}' only runs inside a live voice "
                "session — its result is spoken back mid-conversation; "
                "it is not available on this call path")}
        raise ValueError(
            f"invalid ada_ops action {action!r}: expected "
            "outcome|usage|health|check|research|report|acl_explain")

    def _ops_acl_explain(
            self, identity: str | None, entity_id: str | None,
            bank: str | None, tool: str | None) -> dict[str, Any]:
        """'Who can do what' explain path (card ada-acl-explain): runs the
        unified resolver (MemoryBankRegistry.acl_decision) for
        identity × subject and annotates the runner-side gates that sit
        around it (ADA_READ_ONLY, guest deny-pattern, dangerous-device
        confirm, rate limits) — the answer to "why can kk control the
        TV" without opening three files."""
        ident = str(identity or "").strip() or self.policy_identity()
        entity = str(entity_id or "").strip()
        bank_name = str(bank or "").strip()
        tool_name = str(tool or "").strip() or None
        if not entity and not bank_name:
            raise ValueError(
                "action 'acl_explain' needs a subject: entity_id=<ha "
                "entity> for actuation and/or bank=<memory bank> "
                "(+tool=<tool name> for a bank write)")
        decision = self.banks.acl_decision(
            ident, entity_id=entity or None,
            bank=bank_name or None, tool=tool_name,
            tool_aliases=_ALIASES)
        runtime = self._acl_runtime_gates(decision, entity, tool_name)
        if runtime:
            decision["runtime_gates"] = runtime
        denied_runtime = any(g.get("result") == "deny" for g in runtime)
        confirm_runtime = any(g.get("result") == "confirm" for g in runtime)
        if not decision["allowed"] or denied_runtime:
            decision["verdict"] = "denied"
        elif confirm_runtime or decision.get("requires_confirm"):
            decision["verdict"] = "needs_confirmation"
        else:
            decision["verdict"] = "allowed"
        decision["identity_label"] = self.banks.identity_label(ident)
        return decision

    def _acl_runtime_gates(
            self, decision: dict[str, Any], entity: str,
            tool: str | None) -> list[dict[str, Any]]:
        """Call-time gates the resolver doesn't own — evaluated against
        live runner state so the explain output reflects what would
        actually happen, not just the policy stores."""
        gates: list[dict[str, Any]] = []
        if (os.environ.get("ADA_READ_ONLY") == "true"
                and decision.get("subject") in ("control", "bank_write")):
            gates.append({"layer": "env", "rule": "ADA_READ_ONLY",
                          "result": "deny",
                          "detail": "writes/actuation disabled by "
                                    "ADA_READ_ONLY=true"})
        if decision.get("subject") != "control" or not entity:
            return gates
        if self.chaba is not None and re.search(
                r"(?:lock|cover|button|gate|garage|door|siren|alarm)",
                entity):
            gates.append({"layer": "guest_mode", "rule": "deny_pattern",
                          "result": "deny",
                          "detail": "guest mode: locks/covers/gates are "
                                    "off-limits regardless of policy"})
        if self.memory is not None:
            safety = self.memory._safety.get(entity)
            if safety != "dangerous" and self.memory._safety:
                object_id = entity.split(".", 1)[-1]
                for eid, level in self.memory._safety.items():
                    if (level == "dangerous" and object_id.startswith(
                            eid.split(".", 1)[-1] + "_")):
                        safety = "dangerous"
                        break
            if safety == "dangerous":
                gates.append({"layer": "safety_map",
                              "rule": "dangerous", "result": "confirm",
                              "detail": f"{entity} is marked dangerous — "
                                        "call needs confirmed=true"})
            elif not self.memory._safety:
                gates.append({"layer": "safety_map", "rule": "unloaded",
                              "result": "info",
                              "detail": "safety map not loaded yet — "
                                        "dangerous-confirm gate not "
                                        "evaluated"})
        now = time.monotonic()
        recent = [t for t in self._control_calls
                  if now - t < CONTROL_RATE_WINDOW_S]
        per_entity = [t for t in self._control_entity_calls.get(entity, [])
                      if now - t < CONTROL_RATE_WINDOW_S]
        gates.append({
            "layer": "rate_limit", "rule": "control_rate",
            "result": ("deny" if (len(recent) >= CONTROL_MAX_GLOBAL
                                  or len(per_entity)
                                  >= CONTROL_MAX_PER_ENTITY)
                       else "pass"),
            "detail": f"{len(recent)}/{CONTROL_MAX_GLOBAL} global and "
                      f"{len(per_entity)}/{CONTROL_MAX_PER_ENTITY} "
                      f"per-entity calls in the last "
                      f"{int(CONTROL_RATE_WINDOW_S)}s",
        })
        return gates

    async def _ops_report(
            self, node: str | None, depth: str | None) -> dict[str, Any]:
        """Report-graph freshness + live refresh (chaba report-api on
        tony-dell:8792). Returns the current summary state and kicks the
        subtree walk — Ada answers with the cached numbers and the
        update lands behind her; a 'report-updated' ops event follows
        when it finishes (chaba docs/design/report-live-refresh.md)."""
        import httpx
        base = os.environ.get(
            "REPORT_API_URL", "http://100.68.142.13:8792").rstrip("/")
        token = os.environ.get("REPORT_API_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        node = str(node or "system-report")
        depth = str(depth or "subtree")
        if depth not in ("leaf", "subtree", "full"):
            depth = "subtree"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                status = (await client.get(f"{base}/status")).json()
                resp = await client.post(
                    f"{base}/refresh",
                    json={"node": node, "depth": depth},
                    headers=headers)
        except Exception as exc:
            return {"error": f"report-api unreachable: {exc}"}
        out: dict[str, Any] = {
            "node": node,
            "generated_at": status.get("generated_at"),
            "running": status.get("running"),
        }
        if resp.status_code == 202:
            out["refresh"] = ("started — say the cached numbers are from "
                              "the last run and fresh ones are coming")
        elif resp.status_code == 409:
            out["refresh"] = "already running"
        elif resp.status_code == 403:
            out["refresh"] = "not authorized from this host"
        else:
            out["refresh"] = f"failed ({resp.status_code})"
        return out

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
