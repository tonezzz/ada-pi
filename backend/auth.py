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


def _key_entries() -> dict[str, dict]:
    """File-issued keys normalized to {name: {"key": str, "device": str|None}}."""
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = {}
    for name, value in data.items():
        if isinstance(value, dict):
            key, device = value.get("key"), value.get("device")
        else:
            key, device = value, None
        if name and key:
            entries[str(name)] = {"key": str(key), "device": device or None}
    return entries


def _file_keys() -> dict[str, str]:
    """Dynamically issued keys from the keys file: {key: name}."""
    return {e["key"]: n for n, e in _key_entries().items()}


def _parse_keys() -> dict[str, str]:
    """Return {key: name} for every configured key (env + issued file keys)."""
    keys: dict[str, str] = {}
    single = os.environ.get("ADA_API_KEY", "").strip()
    if single:
        keys[single] = "default"
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


def _save_file_keys(data: dict[str, str]) -> None:
    path = _keys_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)


def create_key(name: str) -> str | None:
    """Issue a new named device key, persisted to the keys file. None if taken."""
    if not _KEY_NAME_RE.match(name):
        return None
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        data = {}
    env_names = {n for n in _parse_keys().values()}
    if name in data or name in env_names:
        return None
    key = f"ada-{secrets.token_urlsafe(24)}"
    data[name] = key
    _save_file_keys(data)
    return key


def revoke_key(name: str) -> bool:
    """Remove a file-issued key. Existing sessions for that name die too."""
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        return False
    if name not in data:
        return False
    del data[name]
    _save_file_keys(data)
    return True


def issued_key_names() -> list[str]:
    return sorted(_key_entries())


def issued_key_bindings() -> dict[str, str | None]:
    """{name: bound_device_id_or_None} for the admin pair page."""
    return {n: e["device"] for n, e in _key_entries().items()}


def bound_device(name: str) -> str | None:
    entry = _key_entries().get(name)
    return entry["device"] if entry else None


def bind_device(name: str, device_id: str) -> bool:
    """Lock an issued key to a device id. False if no such file-issued key."""
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        return False
    if name not in data:
        return False
    key = data[name]["key"] if isinstance(data[name], dict) else data[name]
    data[name] = {"key": key, "device": device_id}
    _save_file_keys(data)
    return True


def unbind_device(name: str) -> bool:
    """Drop a key's device binding (re-pair resets it so a device can re-register).

    A '*' binding means the key is deliberately shared (e.g. the cms-viewer
    iframe key) — re-pair must NOT collapse it back to TOFU or the next
    browser silently re-binds it. Preserve '*' across re-pairs.
    """
    try:
        data = json.loads(open(_keys_file()).read())
    except (OSError, ValueError):
        return False
    if not isinstance(data.get(name), dict):
        return name in data
    if data[name].get("device") == "*":
        return True
    data[name] = data[name]["key"]
    _save_file_keys(data)
    return True


def enforce_device(name: str, device_id: str) -> str | None:
    """Apply the device binding for file-issued keys; return name or None.

    Env/admin keys are never bound. Unbound issued keys trust-on-first-use:
    the first request carrying a device id binds it. Requests missing the
    device id on a bound key are rejected.
    """
    if name not in _key_entries():
        return name
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
    return bool(_parse_keys())


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


def websocket_authorized(ws: Any) -> bool:
    keys = _parse_keys()
    if not keys:
        return True
    provided = ws.query_params.get("api_key") or ""
    name = keys.get(provided) if provided else None
    if name is None:
        session = ws.cookies.get(SESSION_COOKIE, "")
        name = _check_session(session, keys) if session else None
    if name is None:
        return False
    return enforce_device(name, _presented_device({}, ws.query_params)) is not None
