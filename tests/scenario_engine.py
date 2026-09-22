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
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend import memory_ops
from backend.conversation_memory import ConversationMemory
from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import ToolRunner

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

    def _coll(self, name: str) -> dict[str, dict[str, Any]]:
        return self.collections.setdefault(name, {})

    async def add_document(self, collection, key, lang, content_md, meta=None, timeout=None):
        self._coll(collection)[str(key)] = {
            "key": str(key),
            "contentMd": str(content_md),
            "meta": dict(meta or {}),
        }
        return {"key": str(key)}

    async def get_document(self, collection, key, lang="en"):
        doc = self._coll(collection).get(str(key))
        return dict(doc) if doc else None

    async def update_document(self, collection, key, lang="en", content_md=None, meta=None):
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
    conv = ConversationMemory(session_id="scenario")
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
    for i, step in enumerate(data.get("steps") or []):
        entry: dict[str, Any] = {"i": i, "step": step}
        try:
            if "say" in step:
                conv.add_user(str(step["say"]))
                result = {"output": "recorded"}
            elif "assistant" in step:
                conv.add_assistant(str(step["assistant"]))
                result = {"output": "recorded"}
            elif "expand_query" in step:
                result = {"expanded": conv.expand_query(str(step["expand_query"]))}
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
            elif "tool" in step:
                try:
                    result = await runner.execute(
                        str(step["tool"]), dict(step.get("args") or {})
                    )
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                result = {"error": "unknown step kind"}
        except Exception as exc:  # engine bug safety net
            result = {"error": f"engine: {exc}"}
        entry["result"] = result
        entry["failures"] = check_expect(result, step.get("expect") or {})
        report["steps"].append(entry)
        report["failures"].extend(
            f"step {i} ({step.get('tool') or next(iter(step))}): {f}"
            for f in entry["failures"]
        )
    report["ok"] = not report["failures"]
    return report
