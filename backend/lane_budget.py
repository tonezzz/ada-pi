"""Per-lane monthly spend enforcement for the paid Gemini lane.

(card ada-gemini-live-spend-cap, 2026-10-15) The provider-side monthly
spend cap (ws close 1011 'Your project has exceeded its monthly spending
cap — ai.studio/spend') used to be discovered only mid-connect: every
retry paid Google for a doomed handshake and surfaced a cryptic vendor
string. Two enforcement layers now sit in front of the Live connect:

1. Lane budget — ADA_LANE_MONTHLY_BUDGET_USD sets this lane's estimated
   monthly spend ceiling. Spend is derived from the durable usage ledger
   (ADA_USAGE_LOG, usage_tracker.py — per-record est cost, lane-tagged).
   check() raises LaneBudgetExceeded before the session is even
   configured, so an over-budget lane fails fast and free instead of
   burning into the provider cap. ADA_LANE_BUDGET_WARN_PCT (default 80)
   emits a throttled ops event as the lane approaches the wall. Unset/0
   disables the budget gate — enforcement is opt-in per lane.
2. Provider-cap latch — when a connect dies on a billing-class error
   (monthly spending cap 1011, or 402 prepayment-depleted), the key
   fingerprint is latched in ADA_LANE_BUDGET_STATE (default
   data/lane-budget.json) for the current billing month. Later connects
   raise ProviderCapLatched locally — no Google round-trip, no retry
   storm. The latch clears itself on month rollover, when the env key
   changes (different fingerprint), or when the state file is deleted;
   ADA_LANE_CAP_LATCH=off disables learning entirely.

Both exceptions carry .hold_s — live_manager and the pwa reconnect loop
sleep that long (ADA_LANE_BUDGET_RECHECK_S, default 900s) instead of the
normal 1-15s backoff, and surface a 'budget_hold'/'live_budget_hold'
state so the UI can say voice is suspended, not broken. Billing month
follows the Pacific boundary, same convention as the free-tier reset in
gemini_pool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("lane_budget")

# Billing-class provider errors — a lane whose project is capped or out of
# prepaid credit fails every connect the same way, so latch and fail fast.
# 1011 alone is NOT matched (generic ws internal error); the vendor text is.
_BILLING_CAP_RE = re.compile(
    r"spending cap|ai\.studio/spend|prepayment|credits?\s+depleted",
    re.I)


def is_billing_cap_error(exc: BaseException) -> bool:
    """True when the connect failure is a project billing state (monthly
    spend cap or prepaid credits out) that persists until console action —
    not a transient drop."""
    if _BILLING_CAP_RE.search(str(exc)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 402


class _LaneHold(RuntimeError):
    """Base for enforcement holds — .hold_s tells reconnect loops to park
    instead of spinning the normal backoff."""

    code = "lane_hold"

    def __init__(self, message: str, hold_s: float | None = None) -> None:
        super().__init__(message)
        self.hold_s = recheck_s() if hold_s is None else hold_s


class LaneBudgetExceeded(_LaneHold):
    """The lane's estimated monthly spend hit ADA_LANE_MONTHLY_BUDGET_USD."""

    code = "lane_budget"


class ProviderCapLatched(_LaneHold):
    """A billing-cap provider error was already seen for this key this
    month — fail locally instead of paying for another doomed connect."""

    code = "provider_cap"


def hold_seconds(exc: BaseException) -> float | None:
    """Reconnect-loop helper: park duration for enforcement holds, None
    for ordinary failures (caller keeps its normal backoff)."""
    return getattr(exc, "hold_s", None)


# ------------------------------------------------------------------- config


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def budget_usd() -> float:
    """Configured lane ceiling; <=0/unset means the budget gate is off."""
    return _env_float("ADA_LANE_MONTHLY_BUDGET_USD", 0.0)


def warn_pct() -> float:
    return _env_float("ADA_LANE_BUDGET_WARN_PCT", 80.0)


def recheck_s() -> float:
    return _env_float("ADA_LANE_BUDGET_RECHECK_S", 900.0)


def latch_enabled() -> bool:
    return os.environ.get("ADA_LANE_CAP_LATCH", "").strip().lower() not in (
        "0", "off", "false", "no")


def _state_path() -> Path:
    return Path(os.environ.get(
        "ADA_LANE_BUDGET_STATE", "data/lane-budget.json"))


def _usage_log_path() -> Path | None:
    raw = os.environ.get("ADA_USAGE_LOG", "data/usage.jsonl")
    if str(raw).strip().lower() in ("", "off", "none"):
        return None
    return Path(str(raw))


def lane() -> str:
    try:
        from backend.instance import ada_instance_id
        return ada_instance_id()
    except Exception:
        return ""


def key_fingerprint(api_key: str | None) -> str:
    """Stable key identity for latch matching — never logs key material."""
    return hashlib.sha256((api_key or "").encode()).hexdigest()[:10]


# ------------------------------------------------------------------- period


def current_period() -> str:
    """Billing month 'YYYY-MM' on the Pacific boundary — same convention
    as the free-tier daily reset in gemini_pool."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")
    except Exception:
        return datetime.now().strftime("%Y-%m")


def period_end(period: str | None = None) -> str:
    """First day of the month after `period` — the rollover date shown in
    hold messages."""
    period = period or current_period()
    year, month = int(period[:4]), int(period[5:7])
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01"


# ------------------------------------------------------------- spend ledger


_spend_cache: dict[str, Any] = {"key": None, "lane": "", "period": "",
                                "usd": 0.0}


def month_spend_usd(lane_name: str | None = None,
                    period: str | None = None) -> float:
    """Estimated USD this lane has burned this billing month, summed from
    the durable usage ledger. Records carry a 'lane' tag; rows without
    one (pre-tag ledger lines, or a file shared across services) count
    toward every lane — conservative overcount that trips enforcement
    early rather than under-counting a shared file. Cached on file stat
    so retry loops don't re-scan."""
    lane_name = lane() if lane_name is None else lane_name
    period = period or current_period()
    path = _usage_log_path()
    if path is None:
        return 0.0
    try:
        st = path.stat()
    except OSError:
        return 0.0
    ck = (str(path), st.st_size, st.st_mtime_ns)
    if (_spend_cache["key"] == ck and _spend_cache["lane"] == lane_name
            and _spend_cache["period"] == period):
        return float(_spend_cache["usd"])
    from backend.usage_tracker import entry_cost_usd
    total = 0.0
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("ts") or "")[:7] != period:
                    continue
                rec_lane = rec.get("lane")
                if rec_lane is not None and str(rec_lane) != lane_name:
                    continue
                total += entry_cost_usd(rec)
    except OSError:
        return 0.0
    _spend_cache.update(key=ck, lane=lane_name, period=period,
                        usd=round(total, 4))
    return float(_spend_cache["usd"])


# ------------------------------------------------------------- cap latches


def _load_state() -> dict[str, Any]:
    try:
        state = json.loads(_state_path().read_text())
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("lane-budget state write failed (%s): %s", path, exc)


def _cap_for(fp: str, period: str) -> dict[str, Any] | None:
    entry = (_load_state().get("caps") or {}).get(fp)
    if not isinstance(entry, dict):
        return None
    if entry.get("period") != period:
        return None  # last month's latch — billing period rolled over
    return entry


def _emit(emit: Callable[..., Any] | None, ev_type: str,
          detail: str) -> None:
    if emit is None:
        return
    try:
        emit(ev_type, detail)
    except Exception:
        logger.debug("lane-budget emit failed", exc_info=True)


def note_connect_failure(exc: BaseException, api_key: str | None = None,
                         emit: Callable[..., Any] | None = None) -> bool:
    """Learn a billing-class connect failure into the latch file so later
    connects fail locally. Returns True when a latch was written."""
    if not latch_enabled() or not is_billing_cap_error(exc):
        return False
    lane_name = lane()
    period = current_period()
    key = api_key or os.environ.get("GEMINI_API_KEY") or ""
    fp = key_fingerprint(key)
    state = _load_state()
    caps = state.setdefault("caps", {})
    if fp in caps and caps[fp].get("period") == period:
        return True  # already latched this month
    caps[fp] = {
        "lane": lane_name,
        "period": period,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": str(exc)[:200],
    }
    # Drop stale latches from earlier periods — the file stays small.
    for k in [k for k, v in caps.items()
              if isinstance(v, dict) and v.get("period") != period]:
        caps.pop(k, None)
    _save_state(state)
    detail = (
        f"gemini live connect hit a provider billing cap — lane "
        f"'{lane_name}' latches key …{fp[:4]} for {period} and fails "
        f"voice connects locally until {period_end(period)} rollover, a "
        f"rekey, or {_state_path()} is removed: "
        f"{type(exc).__name__}: {exc}"[:300])
    logger.warning("%s", detail)
    _emit(emit, "provider_cap_latched", detail)
    return True


# -------------------------------------------------------------------- gate


def check(api_key: str | None = None, kind: str = "live",
          emit: Callable[..., Any] | None = None) -> None:
    """Connect-time enforcement. Raises ProviderCapLatched when this key's
    project already reported a billing cap this month, then
    LaneBudgetExceeded when the lane's estimated spend passed the
    configured ceiling. No-op when neither applies."""
    lane_name = lane()
    period = current_period()
    key = api_key or os.environ.get("GEMINI_API_KEY") or ""
    latched = _cap_for(key_fingerprint(key), period)
    if latched is not None:
        raise ProviderCapLatched(
            f"Gemini project for lane '{lane_name}' is over its monthly "
            f"billing cap (provider error latched {latched.get('at')}: "
            f"{str(latched.get('reason'))[:120]}). Voice sessions held "
            f"until {period_end(period)} rollover, a rekey/top-up, or "
            f"clearing {_state_path()}.")
    cap = budget_usd()
    if cap <= 0:
        return
    spent = month_spend_usd(lane_name, period)
    if spent >= cap:
        detail = (
            f"lane '{lane_name}' monthly {kind} budget reached — est "
            f"${spent:.2f} of ${cap:.2f} for {period}")
        logger.warning("%s", detail)
        _emit(emit, "lane_budget_block", detail)
        raise LaneBudgetExceeded(
            f"{detail}. Voice sessions held until {period_end(period)} "
            f"or ADA_LANE_MONTHLY_BUDGET_USD is raised.")
    warn_at = cap * warn_pct() / 100.0
    if spent >= warn_at:
        _warn_once(lane_name, period, emit, spent, cap)


def _warn_once(lane_name: str, period: str,
               emit: Callable[..., Any] | None, spent: float,
               cap: float) -> None:
    state = _load_state()
    warned = state.setdefault("warned", {})
    key = f"{lane_name}:{period}"
    if warned.get(key):
        return
    warned[key] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_state(state)
    _emit(emit, "lane_budget_warn",
          f"lane '{lane_name}' at est ${spent:.2f} of ${cap:.2f} monthly "
          f"budget ({warn_pct():.0f}% warn line) for {period} — "
          f"voice holds at ${cap:.2f}")


# ------------------------------------------------------------------ status


def status() -> dict[str, Any]:
    """Snapshot for conversation_health — budget, spend and latches.
    Never carries key material (fingerprints only)."""
    lane_name = lane()
    period = current_period()
    cap = budget_usd()
    spent = month_spend_usd(lane_name, period)
    caps = {
        fp: dict(entry)
        for fp, entry in (_load_state().get("caps") or {}).items()
        if isinstance(entry, dict)
    }
    return {
        "lane": lane_name,
        "period": period,
        "period_end": period_end(period),
        "budget_usd": cap or None,
        "warn_pct": warn_pct(),
        "spent_usd": spent,
        "remaining_usd": round(cap - spent, 4) if cap > 0 else None,
        "latch_enabled": latch_enabled(),
        "latched_caps": caps,
        "state_file": str(_state_path()),
    }


def reset() -> None:
    """Test hook — clear the spend cache (state file is per-test env)."""
    _spend_cache.update(key=None, lane="", period="", usd=0.0)
