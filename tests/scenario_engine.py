"""Scenario test engine for Ada's memory/tooling layer.

Two execution surfaces share this module:

- tests/test_scenarios.py runs every tests/scenarios/*.yaml offline against
  a ToolRunner wired to an in-memory FakeMddb — deterministic, no network.
- scripts/scenario-live.py drives real /ws text turns against a live service
  and applies the same expectation vocabulary to ws trace events.

A scenario file looks like:

    name: michael-technician
    instance: michael
    seed:
      - bank: home
        key: runbook/ada-ha-michael
        text: "ada-ha-michael runs on mn01:8003; restart with
               systemctl --user restart ada-ha-michael"
        meta: {kind: [procedure], subject: [ada-ha-michael]}
    steps:
      - tool: ada_memory_search
        args: {query: "ada-ha-michael dashboard not loading"}
        expect: {count_min: 1, hits_contain: ["restart"]}
      - say: "it's the ada-ha-michael one"          # feeds conversation ctx
      - expand_query: "the other one"
        expect: {expanded_contains: ["ada-ha-michael"]}
      - reconnect_after: 3h          # simulate ws disconnect+reconnect;
        expect: {output_contains: ['"tier": "return"']}   # null = first session

Steps may also carry `capture: {name: "regex"}` — the first regex group is
stored and `{name}` placeholders in later steps' args are substituted
(e.g. replay a confirm_token minted by an earlier denial). `audit:
confirmations` snapshots the runner's structured confirm ledger.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend import memory_ops
from backend.conversation_memory import ConversationMemory
from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import ToolRunner
from backend.usage_tracker import usage_ledger

# Registry mirror of the deployed bank set (ssot.apps.ada-memory-banks.yml),
# trimmed to what the tools exercise. Scenario files may override with a
# `banks:` block of their own.
SCENARIO_BANKS: dict[str, Any] = {
    "banks": {
        "general": {
            "title": "General knowledge",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-general",
            "notebooklm_group": "memory",
            "kinds": ["fact", "preference", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "home": {
            "title": "Household knowledge",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-home",
            "kinds": ["fact", "procedure", "note"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "infrastructure-ssot": {
            "title": "Infrastructure SSOT",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "infrastructure-ssot",
            "kinds": ["fact", "procedure", "note"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "people": {
            "title": "People",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-people",
            "kinds": ["person", "fact", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "personal": {
            "title": "Personal",
            "scope": "instance",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-personal-{instance}",
            "kinds": ["fact", "preference", "person", "procedure", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "note": {
            "title": "Scratch notes (test bank)",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-note-{instance}",
            "kinds": ["note", "fact"],
            "writable": True,
            "write_policy": "direct",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "testing",
        },
        "tony-projects": {
            "title": "Tony's projects",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-projects-tony",
            "kinds": ["note", "fact", "procedure"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
    }
}

_AWAY_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d|w)?$")
_AWAY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_away(raw: Any) -> float | None:
    """'90s'/'5m'/'4h'/'2d'/'1w' or bare seconds; null/'none' = first session."""
    if raw is None or str(raw).strip().lower() in ("", "none", "null"):
        return None
    m = _AWAY_RE.match(str(raw).strip().lower())
    if not m:
        raise ValueError(f"bad reconnect_after duration {raw!r}")
    return float(m.group(1)) * _AWAY_UNITS.get(m.group(2) or "s", 1)


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "is", "it", "to", "of", "and", "or", "in", "on",
    "what", "who", "how", "do", "does", "for", "about", "me", "my", "i",
}


class FakeMddb:
    """In-memory MDDB: real add/get/update semantics plus a keyword-overlap
    'vector' search so scenario steps exercise the genuine find-then-update
    and draft-filtering logic in memory_ops."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}

    def is_ops_routed(self, collection: str) -> bool:
        # MddbClient marks ops/scenario-report collections as ops-routed so
        # status_outcome docs skip the 'active' status gate — the fake has
        # no such routing, plain collections only.
        return False

    def _coll(self, name: str) -> dict[str, dict[str, Any]]:
        return self.collections.setdefault(name, {})

    async def add_document(self, collection, key, lang, content_md, meta=None, timeout=None,
                           durable=False, tool="", session_id=None):
        self._coll(collection)[str(key)] = {
            "key": str(key),
            "contentMd": str(content_md),
            "meta": dict(meta or {}),
        }
        return {"key": str(key)}

    async def get_document(self, collection, key, lang="en"):
        doc = self._coll(collection).get(str(key))
        return dict(doc) if doc else None

    async def update_document(self, collection, key, lang="en", content_md=None, meta=None,
                              durable=False, tool="", session_id=None):
        doc = self._coll(collection).get(str(key))
        if doc is None:
            return None
        if content_md is not None:
            doc["contentMd"] = str(content_md)
        if meta is not None:
            doc["meta"] = dict(meta)
        return {"key": str(key)}

    @staticmethod
    def _meta_match(doc: dict, filter_meta: dict[str, list[str]] | None) -> bool:
        if not filter_meta:
            return True
        meta = doc.get("meta") or {}
        for field, wanted in filter_meta.items():
            have = meta.get(field) or []
            if not isinstance(have, list):
                have = [have]
            if not set(map(str, wanted)) & set(map(str, have)):
                return False
        return True

    async def search_documents(self, collection, query="*", filter_meta=None, limit=10):
        docs = [
            dict(d) for d in self._coll(collection).values()
            if self._meta_match(d, filter_meta)
        ]
        return docs[: int(limit)]

    async def vector_search(self, collection, query, limit=5, filter_meta=None, threshold=0.0):
        terms = {
            t for t in _WORD_RE.findall(str(query).lower()) if t not in _STOP
        }
        out = []
        for d in self._coll(collection).values():
            if not self._meta_match(d, filter_meta):
                continue
            meta = d.get("meta") or {}
            haystack = " ".join(
                [
                    str(d.get("key") or ""),
                    str(d.get("contentMd") or ""),
                    " ".join(map(str, meta.get("subject") or [])),
                    " ".join(map(str, meta.get("attribute") or [])),
                ]
            ).lower()
            hay_terms = set(_WORD_RE.findall(haystack))
            score = len(terms & hay_terms) / len(terms) if terms else 0.0
            if score >= float(threshold or 0):
                out.append({**dict(d), "score": round(score, 3)})
        out.sort(key=lambda d: d["score"], reverse=True)
        return out[: int(limit)]


def build_registry(instance: str, spec: dict | None = None) -> MemoryBankRegistry:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec or SCENARIO_BANKS, f)
        path = f.name
    reg = MemoryBankRegistry(path=path, instance=instance, notebook_ids={"memory": "nb-1"})
    Path(path).unlink(missing_ok=True)
    return reg


def check_expect(result: Any, expect: dict[str, Any]) -> list[str]:
    """Evaluate expectation keys against a step result; returns failures."""
    failures: list[str] = []
    result = result if isinstance(result, dict) else {"output": result}
    blob = json.dumps(result, default=str)
    hits = result.get("hits") or []

    if "verb" in expect and result.get("verb") != expect["verb"]:
        failures.append(f"verb: want {expect['verb']!r}, got {result.get('verb')!r}")
    if expect.get("no_error") and result.get("error"):
        failures.append(f"unexpected error: {result['error']}")
    if "error_contains" in expect and expect["error_contains"] not in str(result.get("error", "")):
        failures.append(f"error: want substring {expect['error_contains']!r}, got {result.get('error')!r}")
    if "count" in expect and result.get("count") != expect["count"]:
        failures.append(f"count: want {expect['count']}, got {result.get('count')}")
    if "count_min" in expect and (result.get("count") or 0) < expect["count_min"]:
        failures.append(f"count: want >= {expect['count_min']}, got {result.get('count')}")
    for sub in expect.get("hits_contain") or []:
        if not any(sub in str(h.get("content") or "") for h in hits):
            failures.append(f"hits: no hit contains {sub!r}")
    for bank in expect.get("hit_banks") or []:
        if not any(h.get("bank") == bank for h in hits):
            failures.append(f"hits: no hit from bank {bank!r}")
    if "draft_hits_min" in expect:
        n = sum(1 for h in hits if h.get("draft"))
        if n < expect["draft_hits_min"]:
            failures.append(f"draft hits: want >= {expect['draft_hits_min']}, got {n}")
    for sub in expect.get("expanded_contains") or []:
        if sub not in str(result.get("expanded") or ""):
            failures.append(f"expanded query missing {sub!r}")
    for sub in expect.get("output_contains") or []:
        if sub not in blob:
            failures.append(f"output missing {sub!r}")
    for sub in expect.get("output_not_contains") or []:
        if sub in blob:
            failures.append(f"output unexpectedly contains {sub!r}")
    return failures


async def run_scenario(path: str | Path) -> dict[str, Any]:
    """Execute one scenario file offline; returns a report dict."""
    data = yaml.safe_load(Path(path).read_text())
    instance = str(data.get("instance") or "test")
    registry = build_registry(instance, data.get("banks"))
    fake = FakeMddb()
    runner = ToolRunner(unittest.mock.AsyncMock(), instance_id=instance)
    runner._banks = registry
    runner.mddb = fake
    # `persons:` declares the HA people resolve_person()/persons() should
    # see — matches by entity_id, friendly name, or name slug.
    people = [dict(p) for p in (data.get("persons") or [])]
    if people:
        def _norm(s: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")

        async def _resolve_person(name: Any) -> dict | None:
            n = str(name or "").strip().lower()
            for p in people:
                if n in (str(p.get("entity_id") or "").lower(),
                         str(p.get("name") or "").lower()):
                    return dict(p)
            for p in people:
                if f"person.{_norm(p.get('name'))}" == n:
                    return dict(p)
            return None

        async def _persons() -> list[dict]:
            return [dict(p) for p in people]

        runner.context.ha_client.resolve_person = _resolve_person
        runner.context.ha_client.persons = _persons

    # `env:` sets os.environ for the scenario duration (restored after).
    env_spec = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    # `cast_screens:` writes the registry to a temp file and points
    # ADA_CAST_SCREENS at it — screen ownership + the tv block.
    tmp_files: list[str] = []
    if data.get("cast_screens") is not None:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(data["cast_screens"], f)
            tmp_files.append(f.name)
        env_spec["ADA_CAST_SCREENS"] = f.name
    saved_env = {k: os.environ.get(k) for k in env_spec}
    os.environ.update(env_spec)

    # `ha_states:` / `ha_states_seq:` stub ha_client.get_state —
    # entity_id -> HA state doc ({"state","attributes"}); seq entries are
    # consumed in call order, the last one repeats.
    ha_states = data.get("ha_states") or {}
    ha_seqs = {k: list(v) for k, v in
               (data.get("ha_states_seq") or {}).items()}
    if ha_states or ha_seqs:
        async def _get_state(entity_id):
            seq = ha_seqs.get(str(entity_id))
            if seq:
                return dict(seq.pop(0) if len(seq) > 1 else seq[0])
            st = ha_states.get(str(entity_id))
            if isinstance(st, dict):
                return dict(st)
            return {"entity_id": entity_id, "state": "unknown"}
        runner.context.ha_client.get_state = _get_state
    # `ha_tv_action:` return value for ha_client.tv_action (the
    # cast-browser body).
    if "ha_tv_action" in data:
        tv_res = data["ha_tv_action"]
        runner.context.ha_client.tv_action = unittest.mock.AsyncMock(
            return_value=dict(tv_res) if isinstance(tv_res, dict)
            else tv_res)
    # `vcast:` stubs ToolRunner._vcast_api — path -> canned response.
    vcast_stub = data.get("vcast")
    if isinstance(vcast_stub, dict):
        def _fake_vcast(path, payload=None):
            resp = vcast_stub.get(path)
            if isinstance(resp, dict):
                if set(resp) == {"error"}:
                    raise RuntimeError(str(resp["error"]))
                return dict(resp)
            return resp if resp is not None else {}
        runner._vcast_api = _fake_vcast
    conv = ConversationMemory(session_id="scenario")
    usage_ledger.configure(None)  # no data/usage.jsonl writes in offline runs
    usage_ledger.reset()
    today = datetime.now(timezone.utc).date().isoformat()

    for i, seed in enumerate(data.get("seed") or []):
        if "bank" in seed:
            bank = registry.bank(str(seed["bank"]))
            collection = bank.mddb_collection
            default_scope = instance if bank.scope == "instance" else "shared"
        else:
            collection = str(seed["collection"])
            default_scope = "shared"
        meta = {
            "status": ["active"],
            "valid_from": [today],
            "last_verified": [today],
            "scope": [default_scope],
            "kind": ["note"],
            "source": ["scenario"],
        }
        for k, v in (seed.get("meta") or {}).items():
            meta[k] = [today if str(x) == "__TODAY__" else x for x in (v if isinstance(v, list) else [v])]
        await fake.add_document(
            collection, str(seed.get("key") or f"seed/{i}"), "en",
            str(seed["text"]), meta,
        )

    report: dict[str, Any] = {
        "name": data.get("name") or Path(path).stem,
        "instance": instance,
        "steps": [],
        "failures": [],
    }
    captures: dict[str, str] = {}

    def _subst(value: Any) -> Any:
        if isinstance(value, str):
            for key, stored in captures.items():
                value = value.replace("{" + key + "}", stored)
            return value
        if isinstance(value, dict):
            return {k: _subst(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_subst(v) for v in value]
        return value

    def _restore_env() -> None:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in tmp_files:
            Path(p).unlink(missing_ok=True)

    try:
      for i, step in enumerate(data.get("steps") or []):
        entry: dict[str, Any] = {"i": i, "step": step}
        t0 = time.monotonic()
        try:
            if "say" in step:
                conv.add_user(str(step["say"]))
                result = {"output": "recorded"}
            elif "assistant" in step:
                conv.add_assistant(str(step["assistant"]))
                result = {"output": "recorded"}
            elif "expand_query" in step:
                result = {"expanded": conv.expand_query(str(step["expand_query"]))}
            elif "audit" in step:
                if str(step["audit"]) == "confirmations":
                    entries = runner.confirmation_audit()
                    result = {"count": len(entries), "entries": entries}
                else:
                    result = {"error": f"unknown audit {step['audit']!r}"}
            elif "reconnect_after" in step:
                # Simulate a websocket disconnect+reconnect: snapshot the
                # transcript tail, start a fresh ConversationMemory, and run
                # the real session prime with the simulated gap.
                away = _parse_away(step["reconnect_after"])
                last_tail = conv.recent_context(max_turns=6, max_chars=800)
                conv = ConversationMemory(session_id=f"scenario-r{i}")
                result = {
                    "away_seconds": away,
                    "tier": memory_ops.away_tier(away),
                    "prime": await memory_ops.session_prime_text(
                        runner.mddb,
                        runner.banks,
                        away_seconds=away,
                        last_tail=last_tail,
                    ),
                }
            elif "set" in step:
                # Mutate runner session state — used by session-security
                # scenarios to pin the owner or swap the identified speaker.
                for k, v in dict(step["set"]).items():
                    setattr(runner, str(k), v)
                result = {"output": "set"}
            elif "record_usage" in step:
                usage_ledger.record(**dict(step["record_usage"]))
                result = {"output": "recorded"}
            elif "tool" in step:
                try:
                    result = await runner.execute(
                        str(step["tool"]), _subst(dict(step.get("args") or {}))
                    )
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                result = {"error": "unknown step kind"}
        except Exception as exc:  # engine bug safety net
            result = {"error": f"engine: {exc}"}
        entry["ms"] = int((time.monotonic() - t0) * 1000)
        for var, pattern in (step.get("capture") or {}).items():
            match = re.search(str(pattern), json.dumps(result, default=str))
            if match:
                captures[var] = match.group(1)
        entry["result"] = result
        entry["failures"] = check_expect(result, step.get("expect") or {})
        report["steps"].append(entry)
        report["failures"].extend(
            f"step {i} ({step.get('tool') or next(iter(step))}): {f}"
            for f in entry["failures"]
        )
    finally:
        _restore_env()
    report["ok"] = not report["failures"]
    return report
