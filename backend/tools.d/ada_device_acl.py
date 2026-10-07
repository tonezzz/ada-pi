"""ada_device_acl — owner-only device ACL management.

Runtime overlay on control_policies: grants/revokes land in
~/.config/ada/device-grants.json (ADA_DEVICE_GRANTS_FILE) and take effect
immediately — control_allowed() reloads on mtime. Use it for per-member
device sets in shared rooms (the tony-ha Telegram room): owner grants a
member an entity, and it is allowed even when the member's domain
allowlist wouldn't cover it. deny_entities/deny_domains still beat a grant.

Actions:
  list    — show an identity's effective policy + grants (or all when
            identity omitted)
  grant   — add entity_id to identity's granted set
  revoke  — remove it
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DECLARATION = {
    "name": "ada_device_acl",
    "description": (
        "Manage per-identity device ACL grants — owner only. "
        "action='list'|'grant'|'revoke'; grant/revoke take identity "
        "(e.g. 'person.kk') and entity_id. Grants apply immediately."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'list' (default), 'grant', or 'revoke'.",
            },
            "identity": {
                "type": "string",
                "description": "Target identity, e.g. 'person.kk' or "
                               "'user-kk'.",
            },
            "entity_id": {
                "type": "string",
                "description": "HA entity to grant/revoke, e.g. "
                               "'light.bedroom'.",
            },
        },
        "required": [],
    },
}

GRANTS_PATH = Path(os.environ.get(
    "ADA_DEVICE_GRANTS_FILE",
    str(Path.home() / ".config" / "ada" / "device-grants.json")))


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(GRANTS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(grants: dict) -> None:
    GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRANTS_PATH.write_text(json.dumps(grants, indent=2, ensure_ascii=False)
                           + "\n")


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "list").lower()
    ident = str(args.get("identity") or "").strip()
    entity = str(args.get("entity_id") or "").strip()
    grants = _load()

    if action == "list":
        if ident:
            policy = (runner.banks.control_policy_for(ident) or {})
            return {
                "ok": True, "identity": ident,
                "policy": policy,
                "granted": sorted((grants.get(ident) or {}).keys()),
            }
        return {
            "ok": True,
            "identities": sorted(
                set(runner.banks.control_policies) | set(grants)),
            "grants": {k: sorted(v) for k, v in grants.items()},
        }

    if action in ("grant", "revoke"):
        if not ident or not entity or "." not in entity:
            return {"ok": False,
                    "error": "grant/revoke needs identity and a dotted "
                             "entity_id (e.g. light.bedroom)"}
        who = runner.policy_identity() or "unknown"
        bucket = grants.setdefault(ident, {})
        if action == "grant":
            bucket[entity] = {
                "granted_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                "by": who,
            }
            _save(grants)
            return {"ok": True, "granted": entity, "identity": ident,
                    "effective": runner.banks.control_allowed(entity, ident)}
        bucket.pop(entity, None)
        if not bucket:
            grants.pop(ident, None)
        _save(grants)
        return {"ok": True, "revoked": entity, "identity": ident,
                "effective": runner.banks.control_allowed(entity, ident)}

    return {"ok": False,
            "error": f"unknown action {action!r} — use list|grant|revoke"}
