"""ada_member_keys — owner-only member invite + key approval gate.

Implements the ada-member-invite standard (docs/kb/ada-member-invite.md):
invites mint PENDING keys bound to a person label; they cannot
authenticate until the owner approves — by voice here ("approve kk") or
the Approve button on <ada-keys-card>.

Actions:
  list     — pending / issued / revoked key names (+ person, claimed)
  invite   — mint a pending key + persistent /i/<tok> landing link;
             on a revoked name resurrects it as a fresh pending invite
  approve  — flip a pending key to issued (member's PWA can now redeem)
  reject   — tombstone a pending key (invite link dies)
  revoke   — tombstone an issued key (sessions die)
  reissue  — pending: rotate the invite link (old links die);
             issued: reset device binding + mint a burn-once re-pair link
"""

from __future__ import annotations

import os
from typing import Any

from backend import auth

DECLARATION = {
    "name": "ada_member_keys",
    "description": (
        "Member PWA invites + key approval — owner only. action='list'|"
        "'invite'|'approve'|'reject'|'revoke'|'reissue'; name= key name "
        "or person label."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'list' (default), 'invite', 'approve', "
                               "'reject', 'revoke', or 'reissue'.",
            },
            "name": {
                "type": "string",
                "description": "Key name ('user-kk') or person label "
                               "('kk') — fuzzy-resolved.",
            },
            "person": {
                "type": "string",
                "description": "invite: member display label for the "
                               "ADA-{INSTANCE}({Person}) install name.",
            },
            "ha_person": {
                "type": "string",
                "description": "invite: optional HA person binding "
                               "('person.kk').",
            },
        },
        "required": [],
    },
}


def _resolve(term: str, entries: dict[str, dict]) -> str | None:
    """Fuzzy name match: exact, 'user-'-prefixed, then person label /
    case-insensitive."""
    term = (term or "").strip()
    if not term:
        return None
    if term in entries:
        return term
    if f"user-{term}" in entries:
        return f"user-{term}"
    low = term.lower()
    for n, e in entries.items():
        if n.lower() == low or str(e.get("person") or "").lower() == low:
            return n
    return None


def _public_base() -> str:
    return (os.environ.get("ADA_PUBLIC_URL")
            or os.environ.get("ADA_SELF_URL")
            or "").rstrip("/")


def _invite_url(token: str) -> str:
    base = _public_base()
    return f"{base}/i/{token}" if base else f"/i/{token}"


def _entry_snapshot(name: str) -> dict:
    d = (auth.invite_details(name) or {})
    return {"name": name, "person": d.get("person"),
            "status": d.get("status"), "claimed": d.get("claimed")}


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "list").lower()
    entries = {n: auth.invite_details(n) or {}
               for n in auth.issued_key_names()}

    if action == "list":
        groups = {"pending": [], "issued": [], "revoked": []}
        for n, d in entries.items():
            line = n
            if d.get("person"):
                line += f" ({d['person']})"
            if d.get("claimed"):
                line += " claimed"
            groups.get(d.get("status") or "", groups["issued"]).append(line)
        return {"ok": True, **groups}

    term = str(args.get("name") or "")
    name = _resolve(term, entries)

    if action == "invite":
        person = str(args.get("person") or "").strip() or None
        ha_person = str(args.get("ha_person") or "").strip() or None
        if name and (entries.get(name) or {}).get("status") == "revoked":
            token = auth.reinvite_key(name, person=person)
            return {"ok": True, "reinvited": True,
                    "invite_url": _invite_url(token), **_entry_snapshot(name)}
        key_name = str(args.get("name") or "").strip()
        if not key_name:
            return {"ok": False, "error": "invite needs name= (e.g. user-kk)"}
        if person is None:
            person = key_name.removeprefix("user-").upper()
        if auth.create_key(key_name, ha_person=ha_person, approved=False,
                           person=person, invite=True) is None:
            return {"ok": False,
                    "error": f"can't invite '{key_name}' — name taken or invalid"}
        token = auth.invite_token_for(key_name)
        return {"ok": True, "invite_url": _invite_url(token),
                "note": "send the link; the key stays pending until you approve",
                **_entry_snapshot(key_name)}

    if name is None:
        pending = [n for n, d in entries.items()
                   if d.get("status") == "pending"]
        hint = f" Pending: {', '.join(pending)}." if pending else ""
        return {"ok": False,
                "error": f"no key matching '{term}'.{hint}"}

    status = (entries.get(name) or {}).get("status")

    if action == "approve":
        if status == "issued":
            return {"ok": True, "already": True, **_entry_snapshot(name)}
        if status != "pending":
            return {"ok": False, "error": f"{name} is {status} — can't approve"}
        if not auth.approve_key(name):
            return {"ok": False, "error": f"approve failed for {name}"}
        return {"ok": True, **_entry_snapshot(name),
                "note": "approved — the member's app can finish setup"}

    if action == "reject":
        if status != "pending":
            return {"ok": False,
                    "error": f"{name} is {status} — reject is for pending "
                             "keys; use revoke for issued"}
        auth.revoke_key(name)
        return {"ok": True, **_entry_snapshot(name),
                "note": "rejected — the invite link is dead"}

    if action == "revoke":
        if status == "revoked":
            return {"ok": True, "already": True, **_entry_snapshot(name)}
        if not auth.revoke_key(name):
            return {"ok": False, "error": f"revoke failed for {name}"}
        return {"ok": True, **_entry_snapshot(name),
                "note": "revoked — sessions die on next request"}

    if action == "reissue":
        if status == "pending":
            token = auth.rotate_invite(name)
            return {"ok": True, "invite_url": _invite_url(token),
                    "note": "old invite links are dead",
                    **_entry_snapshot(name)}
        if status == "revoked":
            return {"ok": False,
                    "error": f"{name} is revoked — action=invite re-invites"}
        auth.unbind_device(name)
        token = auth.mint_redeem_token(name, "/")
        base = _public_base()
        return {"ok": True,
                "redeem_url": f"{base}/redeem/{token}" if base
                else f"/redeem/{token}",
                "expires_in": auth.REDEEM_TTL_S,
                "note": "device binding reset — burn-once re-pair link",
                **_entry_snapshot(name)}

    return {"ok": False,
            "error": f"unknown action {action!r} — "
                     "use list|invite|approve|reject|revoke|reissue"}
