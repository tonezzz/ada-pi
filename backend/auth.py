"""Shared-secret API auth for Ada backends.

ADA_API_KEY (single key) and ADA_API_KEYS (name:key comma pairs) gate the
mutating REST endpoints and the /ws voice socket. Verified callers get a
name for audit logging; POST /api/auth/session trades a key for a
short-lived HMAC-signed HttpOnly session cookie so the raw key does not
need to persist in browser storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any

SESSION_COOKIE = "ada_session"
SESSION_TTL_S = int(os.environ.get("ADA_SESSION_TTL_S", str(12 * 3600)))
REDEEM_TTL_S = int(os.environ.get("ADA_REDEEM_TTL_S", "600"))


def _keys_file() -> str:
    inst = os.environ.get("ADA_INSTANCE_ID", "default")
    return os.environ.get(
        "ADA_KEYS_FILE",
        os.path.expanduser(f"~/.config/secrets/ada-ha-{inst}-keys.json"),
    )


# Apps/capabilities a key can carry: voice PWA, text chat page, cms
# viewer — and 'dispatch', which lets the key spawn Devin sessions via
# POST /api/cms/spawn. dispatch is a capability, not a UI: read-only
# viewer keys (e.g. the shared cms-viewer key embedded in iframes) must
# NOT carry it. None/absent on a key means the legacy default
# ["voice", "chat"] — which has neither view nor dispatch.
VALID_KEY_APPS = ("voice", "chat", "view", "dispatch")


def _clean_apps(apps: Any) -> list[str] | None:
    """Normalize an apps value to a deduped list of known app ids, or None."""
    if not isinstance(apps, (list, tuple)):
        return None
    seen = [a for a in dict.fromkeys(str(a) for a in apps) if a in VALID_KEY_APPS]
    return seen or None


def _key_entries() -> dict[str, dict]:
    """File-issued keys normalized to {name: {"key", "device", "issued",
    "apps", "ha_person", "approved", "revoked", "person", "invite",
    "invite_claimed", "revoked_at"}}.

    State fields (docs/kb/ada-member-invite.md): `approved` absent means
    True (legacy keys stay live); `revoked` is a tombstone — the entry is
    kept for audit but the key no longer authenticates."""
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = {}
    for name, value in data.items():
        if isinstance(value, dict):
            key, device, issued = (value.get("key"), value.get("device"),
                                   value.get("issued"))
            apps = _clean_apps(value.get("apps"))
            ha_person = value.get("ha_person")
            approved = value.get("approved", True)
            revoked = bool(value.get("revoked"))
            person = value.get("person")
            invite = value.get("invite")
            claimed = value.get("invite_claimed")
            revoked_at = value.get("revoked_at")
        else:
            key, device, issued, apps, ha_person = value, None, None, None, None
            approved, revoked, person = True, False, None
            invite, claimed, revoked_at = None, None, None
        if name and key:
            entries[str(name)] = {
                "key": str(key), "device": device or None,
                "issued": issued, "apps": apps, "ha_person": ha_person,
                "approved": bool(approved), "revoked": revoked,
                "person": person, "invite": invite,
                "invite_claimed": claimed if isinstance(claimed, dict) else None,
                "revoked_at": revoked_at,
            }
    return entries


def _key_live(entry: dict) -> bool:
    """A key authenticates only when issued: not revoked and approved."""
    return not entry.get("revoked") and entry.get("approved", True)


def _file_keys() -> dict[str, str]:
    """Dynamically issued LIVE keys from the keys file: {key: name}.
    Pending (approved=false) and revoked keys are excluded — they cannot
    authenticate until the approval gate clears."""
    return {e["key"]: n for n, e in _key_entries().items() if _key_live(e)}


def _parse_keys() -> dict[str, str]:
    """Return {key: name} for every configured key (env + issued file keys)."""
    keys: dict[str, str] = {}
    single = os.environ.get("ADA_API_KEY", "").strip()
    if single:
        keys[single] = "admin"
    for pair in os.environ.get("ADA_API_KEYS", "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, key = pair.partition(":")
        name, key = name.strip(), key.strip()
        if name and key:
            keys[key] = name
    keys.update(_file_keys())
    return keys


_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Serializes read-modify-write cycles on the keys file — without it two
# concurrent create/bind calls can each read, mutate, and clobber the other.
_KEYS_LOCK = threading.Lock()


def _save_file_keys(data: dict[str, str]) -> None:
    path = _keys_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)


def create_key(name: str, apps: Any = None,
               ha_person: str | None = None, approved: bool = True,
               person: str | None = None,
               invite: bool = False) -> str | None:
    """Issue a new named user key, persisted to the keys file. None if taken.

    `apps` optionally restricts which UIs/capabilities the key carries
    ("voice", "chat", "view", "dispatch"); unset/empty means the default
    voice+chat.
    ha_person optionally binds the key to a Home Assistant person entity
    (e.g. 'person.kk') — sessions authenticated with this key inherit that
    identity for memory/persona routing until a voiceprint overrides it.
    approved=False mints a pending key (member-invite flow): it cannot
    authenticate until approve_key() clears the gate.
    `person` is the member-facing display label used by the per-invite
    PWA manifest name (ADA-{INSTANCE}({Person})).
    invite=True persists an invite token on the entry — the long-lived
    bearer secret behind the /i/<token> landing page."""
    if not _KEY_NAME_RE.match(name):
        return None
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            data = {}
        env_names = {n for n in _parse_keys().values()}
        if name in data or name in env_names:
            return None
        key = f"ada-{secrets.token_urlsafe(24)}"
        data[name] = {"key": key, "device": None,
                      "issued": time.strftime("%Y-%m-%d")}
        clean = _clean_apps(apps)
        if clean:
            data[name]["apps"] = clean
        if ha_person:
            data[name]["ha_person"] = str(ha_person)
        if not approved:
            data[name]["approved"] = False
        if person:
            data[name]["person"] = str(person)
        if invite:
            data[name]["invite"] = secrets.token_urlsafe(24)
        _save_file_keys(data)
    try:
        from backend.event_log import log_event
        log_event("key-issued", name, "key",
                  "device key created" if approved else
                  "member invite key created (pending approval)")
    except Exception:
        pass
    return key


def approve_key(name: str) -> bool:
    """Clear a pending key's approval gate. True only when it flipped
    pending -> approved (False on no such key, revoked, or already live)."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return False
        entry = data.get(name)
        if not isinstance(entry, dict) or entry.get("revoked"):
            return False
        if entry.get("approved", True):
            return False
        entry["approved"] = True
        _save_file_keys(data)
    try:
        from backend.event_log import log_event
        log_event("key-approved", name, "key", "member key approved")
    except Exception:
        pass
    return True


def revoke_key(name: str) -> bool:
    """Revoke a file-issued key: writes a revoked tombstone (the entry —
    and its audit fields — survives) instead of deleting it. The key
    stops authenticating immediately; existing sessions die on their next
    request because _parse_keys drops revoked entries."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return False
        entry = data.get(name)
        if entry is None:
            return False
        if not isinstance(entry, dict):
            entry = {"key": entry}
            data[name] = entry
        if entry.get("revoked"):
            return False
        entry["revoked"] = True
        entry["revoked_at"] = time.strftime("%Y-%m-%d %H:%M")
        _save_file_keys(data)
    return True


def reinvite_key(name: str, person: str | None = None) -> str | None:
    """Resurrect a revoked tombstone as a fresh pending invite: new invite
    token (old links die), device binding cleared, approved=False.
    Returns the new invite token, or None when the name isn't a revoked
    file-issued key."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return None
        entry = data.get(name)
        if not isinstance(entry, dict) or not entry.get("revoked"):
            return None
        entry.pop("revoked", None)
        entry.pop("revoked_at", None)
        entry["approved"] = False
        entry["device"] = None
        entry.pop("invite_claimed", None)
        entry["invite"] = secrets.token_urlsafe(24)
        if person:
            entry["person"] = str(person)
        _save_file_keys(data)
        return entry["invite"]


def key_status(name: str) -> str | None:
    """'issued' | 'pending' | 'revoked' for a file-issued key, else None."""
    entry = _key_entries().get(name)
    if entry is None:
        return None
    if entry["revoked"]:
        return "revoked"
    return "issued" if entry["approved"] else "pending"


def invite_for_token(token: str) -> str | None:
    """Resolve a persistent invite token to its key name (any state —
    pending/approved/revoked — so a stale link can still answer status)."""
    if not token:
        return None
    for name, e in _key_entries().items():
        inv = e.get("invite")
        if inv and hmac.compare_digest(str(inv), token):
            return name
    return None


def invite_token_for(name: str) -> str | None:
    entry = _key_entries().get(name)
    return entry.get("invite") if entry else None


def rotate_invite(name: str) -> str | None:
    """Mint a fresh invite token for a key, invalidating old links.
    None when the name isn't a file-issued key."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return None
        entry = data.get(name)
        if not isinstance(entry, dict):
            return None
        entry["invite"] = secrets.token_urlsafe(24)
        _save_file_keys(data)
        return entry["invite"]


def claim_invite(name: str, device_id: str) -> None:
    """Record first-touch on an invite: {device, at} once. Informational
    only — the real device binding lands on the key at successful redeem,
    so the Safari->installed-PWA localStorage hop (iOS keeps them
    separate) can't strand the member on a foreign device id."""
    if not device_id:
        return
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return
        entry = data.get(name)
        if not isinstance(entry, dict) or "invite" not in entry:
            return
        if entry.get("invite_claimed"):
            return
        entry["invite_claimed"] = {
            "device": device_id,
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
        _save_file_keys(data)


def invite_details(name: str) -> dict | None:
    """Member-safe invite view for a key name: person label, approval
    state, claim marker — never the raw key or token."""
    e = _key_entries().get(name)
    if e is None:
        return None
    return {"name": name, "person": e.get("person"),
            "ha_person": e.get("ha_person"),
            "status": key_status(name),
            "claimed": bool(e.get("invite_claimed")),
            "claimed_at": (e.get("invite_claimed") or {}).get("at")}


def issued_key_names() -> list[str]:
    return sorted(_key_entries())


def issued_key_bindings() -> dict[str, str | None]:
    """{name: bound_device_id_or_None} for the admin pair page."""
    return {n: e["device"] for n, e in _key_entries().items()}


def issued_key_timeline() -> dict[str, dict]:
    """{name: {issued, device}} — key issuance dates for reports/admin."""
    return {n: {"issued": e["issued"], "device": e["device"]}
            for n, e in _key_entries().items()}


def issued_key_apps() -> dict[str, list[str] | None]:
    """{name: [apps] or None} — None means the legacy voice+chat default."""
    return {n: e["apps"] for n, e in _key_entries().items()}


def issued_key_details() -> dict[str, dict]:
    """{name: {issued, device, apps, ha_person, status, person, ...}} for
    admin listing (no raw keys/tokens). status is issued|pending|revoked —
    the keys card groups on it."""
    return {n: {"issued": e["issued"], "device": e["device"],
                "apps": e.get("apps"), "ha_person": e.get("ha_person"),
                "status": key_status(n), "person": e.get("person"),
                "invited": bool(e.get("invite")),
                "claimed": bool(e.get("invite_claimed")),
                "revoked_at": e.get("revoked_at")}
            for n, e in _key_entries().items()}


def key_can(name: str, app: str) -> bool:
    """True when caller `name`'s key carries `app` in its apps metadata.

    Env-configured keys (ADA_API_KEY 'admin', ADA_API_KEYS pairs) are
    operator keys — they have no apps metadata and are unrestricted.
    File-issued keys need `app` listed; absent apps is the legacy
    ["voice", "chat"] default, which carries neither 'view' nor
    'dispatch'."""
    entry = _key_entries().get(name)
    if entry is None:
        return True
    return app in (entry["apps"] or ["voice", "chat"])


def ha_person_for_key(name: str) -> str | None:
    """HA person entity bound to an issued key, or None."""
    entry = _key_entries().get(name)
    return entry.get("ha_person") if entry else None


def set_key_ha_person(name: str, ha_person: str | None) -> bool:
    """Bind/unbind an HA person entity on an issued key. False if no such key."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return False
        if name not in data:
            return False
        if not isinstance(data[name], dict):
            key = data[name]
            data[name] = {"key": key}
        if ha_person:
            data[name]["ha_person"] = str(ha_person)
        else:
            data[name].pop("ha_person", None)
        _save_file_keys(data)
    return True


def bound_device(name: str) -> str | None:
    entry = _key_entries().get(name)
    return entry["device"] if entry else None


def bind_device(name: str, device_id: str) -> bool:
    """Lock an issued key to a device id. False if no such file-issued key."""
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return False
        if name not in data:
            return False
        old = data[name]
        if isinstance(old, dict):
            entry = dict(old)
            entry["key"] = old["key"]
        else:
            entry = {"key": old}
        entry["device"] = device_id
        data[name] = entry
        _save_file_keys(data)
    return True


def unbind_device(name: str) -> bool:
    """Drop a key's device binding (re-pair resets it so a device can re-register).

    A '*' binding means the key is deliberately shared (e.g. the cms-viewer
    iframe key) — re-pair must NOT collapse it back to TOFU or the next
    browser silently re-binds it. Preserve '*' across re-pairs.
    """
    with _KEYS_LOCK:
        try:
            data = json.loads(open(_keys_file()).read())
        except (OSError, ValueError):
            return False
        if not isinstance(data.get(name), dict):
            return name in data
        if data[name].get("device") == "*":
            return True
        data[name]["device"] = None
        _save_file_keys(data)
    return True


def enforce_device(name: str, device_id: str) -> str | None:
    """Apply the device binding for file-issued keys; return name or None.

    Env/admin keys are never bound. Unbound issued keys trust-on-first-use:
    the first request carrying a device id binds it. Requests missing the
    device id on a bound key are rejected. Pending and revoked keys never
    pass — approval must clear first (docs/kb/ada-member-invite.md).
    """
    entry = _key_entries().get(name)
    if entry is None:
        return name
    if not _key_live(entry):
        return None
    bound = bound_device(name)
    if bound == "*":
        # Shared key (e.g. a viewer URL embedded in a dashboard iframe):
        # device binding is intentionally disabled for this key.
        return name
    if bound is not None:
        return name if device_id and hmac.compare_digest(device_id, bound) else None
    if device_id:
        bind_device(name, device_id)
    return name


def configured() -> bool:
    # Any key material at all — env pairs, live issued keys, even
    # pending/revoked tombstones — means auth is on. A pending-only keys
    # file must NOT read as "unconfigured" (that would fail open).
    return bool(_parse_keys() or _key_entries())


def _sign(name: str, expires: int, key: str) -> str:
    return hmac.new(key.encode(), f"{name}.{expires}".encode(), hashlib.sha256).hexdigest()


def _check_session(token: str, keys: dict[str, str]) -> str | None:
    try:
        name, exp, sig = token.rsplit(".", 2)
        key = next((k for k, n in keys.items() if n == name), None)
        if key is None or not hmac.compare_digest(sig, _sign(name, int(exp), key)):
            return None
        return name if int(exp) > time.time() else None
    except (AttributeError, ValueError):
        return None


def _presented_device(headers: Any, query: Any) -> str:
    return headers.get("x-device-id") or query.get("device_id") or ""


def caller_name(request: Any) -> str | None:
    """Return the authenticated caller name for a request, or None."""
    keys = _parse_keys()
    provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
    name = keys.get(provided) if provided else None
    if name is None:
        session = request.cookies.get(SESSION_COOKIE, "")
        name = _check_session(session, keys) if session else None
    if name is None:
        return None
    return enforce_device(name, _presented_device(request.headers, request.query_params))


def issue_session(key: str) -> tuple[str, str] | None:
    """Return (name, token) when the key is valid, else None."""
    keys = _parse_keys()
    name = keys.get(key.strip())
    if not name:
        return None
    expires = int(time.time()) + SESSION_TTL_S
    return name, f"{name}.{expires}.{_sign(name, expires, key)}"


def issue_session_for_name(name: str) -> str | None:
    """Mint a session token for a known caller name without re-checking the key."""
    keys = _parse_keys()
    key = next((k for k, n in keys.items() if n == name), None)
    if key is None:
        return None
    expires = int(time.time()) + SESSION_TTL_S
    return f"{name}.{expires}.{_sign(name, expires, key)}"


# One-time redeem tokens: an authenticated caller mints a URL that hands the
# real key + a session cookie to whatever device opens it (e.g. a scanned QR).
# token -> (caller name, app base path, redirect path or None, expiry)
_REDEEM_TOKENS: dict[str, tuple[str, str, str | None, float]] = {}


def _prune_redeem() -> None:
    now = time.time()
    for token in [t for t, e in _REDEEM_TOKENS.items() if e[3] < now]:
        _REDEEM_TOKENS.pop(token, None)


def mint_redeem_token(name: str, base_path: str = "/", redirect: str | None = None) -> str:
    _prune_redeem()
    token = secrets.token_urlsafe(24)
    _REDEEM_TOKENS[token] = (name, base_path, redirect, time.time() + REDEEM_TTL_S)
    return token


def peek_redeem_token(token: str) -> tuple[str, str, str | None] | None:
    """Inspect a redeem token WITHOUT burning it. Returns (name, base, redirect)."""
    entry = _REDEEM_TOKENS.get(token)
    if entry is None or entry[3] < time.time():
        return None
    return entry[0], entry[1], entry[2]


def burn_redeem_token(token: str) -> None:
    _REDEEM_TOKENS.pop(token, None)


def key_for_name(name: str) -> str | None:
    return next((k for k, n in _parse_keys().items() if n == name), None)


def redeem_token(token: str) -> tuple[str, str, str, str | None] | None:
    """Burn a one-time redeem token. Returns (name, real_key, base_path, redirect)."""
    entry = _REDEEM_TOKENS.pop(token, None)
    if entry is None or entry[3] < time.time():
        return None
    name, base_path, redirect, _ = entry
    key = key_for_name(name)
    if key is None:
        return None
    return name, key, base_path, redirect


def websocket_caller(ws: Any) -> str | None:
    """Resolve the caller name for a websocket (api_key → name, else session
    cookie → name). None when keys are configured and no identity resolves.
    "" is the anonymous sentinel — only when auth is NOT configured; a
    configured deployment whose live map is empty (e.g. all keys pending)
    must not mint one."""
    keys = _parse_keys()
    if not keys:
        return None if configured() else ""
    provided = ws.query_params.get("api_key") or ""
    name = keys.get(provided) if provided else None
    if name is None:
        session = ws.cookies.get(SESSION_COOKIE, "")
        name = _check_session(session, keys) if session else None
    return name


def websocket_authorized(ws: Any) -> bool:
    if not configured():
        return True
    name = websocket_caller(ws)
    if name is None:
        return False
    return enforce_device(name, _presented_device({}, ws.query_params)) is not None
