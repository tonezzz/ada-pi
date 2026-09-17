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
import os
import secrets
import time
from typing import Any

SESSION_COOKIE = "ada_session"
SESSION_TTL_S = int(os.environ.get("ADA_SESSION_TTL_S", str(12 * 3600)))
REDEEM_TTL_S = int(os.environ.get("ADA_REDEEM_TTL_S", "600"))


def _parse_keys() -> dict[str, str]:
    """Return {key: name} for every configured key."""
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
    return keys


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


def caller_name(request: Any) -> str | None:
    """Return the authenticated caller name for a request, or None."""
    keys = _parse_keys()
    provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
    if provided and (name := keys.get(provided)):
        return name
    session = request.cookies.get(SESSION_COOKIE, "")
    return _check_session(session, keys) if session else None


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
# token -> (caller name, app base path, expiry)
_REDEEM_TOKENS: dict[str, tuple[str, str, float]] = {}


def _prune_redeem() -> None:
    now = time.time()
    for token in [t for t, (_, _, exp) in _REDEEM_TOKENS.items() if exp < now]:
        _REDEEM_TOKENS.pop(token, None)


def mint_redeem_token(name: str, base_path: str = "/") -> str:
    _prune_redeem()
    token = secrets.token_urlsafe(24)
    _REDEEM_TOKENS[token] = (name, base_path, time.time() + REDEEM_TTL_S)
    return token


def redeem_token(token: str) -> tuple[str, str, str] | None:
    """Burn a one-time redeem token. Returns (name, real_key, base_path)."""
    entry = _REDEEM_TOKENS.pop(token, None)
    if entry is None or entry[2] < time.time():
        return None
    name, base_path, _ = entry
    key = next((k for k, n in _parse_keys().items() if n == name), None)
    if key is None:
        return None
    return name, key, base_path


def websocket_authorized(ws: Any) -> bool:
    keys = _parse_keys()
    if not keys:
        return True
    provided = ws.query_params.get("api_key") or ""
    if provided and provided in keys:
        return True
    session = ws.cookies.get(SESSION_COOKIE, "")
    return bool(session and _check_session(session, keys))
