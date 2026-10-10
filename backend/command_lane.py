"""Local-model short-command lane (card use-local-model-for-short-voice-commands).

Short user turns — "turn off the fan", "is the bedroom light on", "what
time is it" — get routed to the local systemone fleet (gemma-3-1b/4b —
the same /v1/systemone surface the jev advisory and orch-bench lanes
use) instead of spending a Gemini Live turn on them.

Two-hop shape, same as the bench `tool` domain:

    1. route   — choice question picks the first action
                 (domains.yml 'tool' criteria + a 'pass' seat for live
                 traffic the bench never sees).
    2. actuate — for 'control', a second choice question picks the
                 entity_id out of the real HA controllable set; the
                 direction verb is extracted deterministically (L0) and
                 the call runs through tool_runner.execute so every
                 existing gate (device ACL, dangerous-entity confirm,
                 rate limits, ADA_READ_ONLY) applies unchanged.

Modes (ADA_COMMAND_LANE):

    off     — lane disabled; nothing is probed or handled.
    shadow  — (default) short turns get classified fire-and-forget; what
              the lane WOULD have done lands in the ops event log and
              the corpus JSONL. Nothing executes — same shadow-first
              lifecycle jev_advisory and the CAM++ speaker shadow used.
    enforce — the lane actually answers: a confident servable route
              executes through the runner and the caller gets the reply.
              Armed for TEXT turns — voice audio streams to Gemini Live
              in real time, so a voice turn can't be withheld from
              Gemini until a local STT stage exists upstream (the
              voice-rainy-day cascade); voice stays advisory in every
              mode.

Corpus rows append to $ADA_COMMAND_LANE_CORPUS
(default ~/.local/share/ada/command-lane-corpus.jsonl) — same directory
convention as jev-corpus.jsonl. They are the promotion evidence: when
route accuracy on live traffic is proven, flip the env to enforce.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("voice.command_lane")

_MODE_ENV = "ADA_COMMAND_LANE"
_URL_ENV = "ADA_COMMAND_LLM_URL"
_TIMEOUT_ENV = "ADA_COMMAND_LANE_TIMEOUT_S"
_MIN_CONF_ENV = "ADA_COMMAND_LANE_MIN_CONF"
_MAX_WORDS_ENV = "ADA_COMMAND_LANE_MAX_WORDS"
_CORPUS_ENV = "ADA_COMMAND_LANE_CORPUS"

_DEFAULT_MAX_WORDS = 12
_DEFAULT_MIN_CONF = 0.65
_DEFAULT_TIMEOUT_S = 8.0
_ENTITY_PICK_LIMIT = 30

# Route choice — production-parity wording with the bench `tool` domain
# (tests/bench/domains.yml) plus 'pass': live traffic carries plenty of
# turns the lane must not swallow, and the bench criteria had no need
# for an escape seat.
_ROUTE_STATE = (
    "Voice assistant Ada received user speech. Pick the single best "
    "action. User said: \"{turn}\"")
_ROUTE_QUESTION = {
    "type": "choice",
    "instructions": "Choose the action Ada should take first.",
    "criteria": {
        "remember": "store the information in long-term memory",
        "recall": "search memory / past sessions to answer",
        "control": "act on a smart-home device (switch, scene, media)",
        "answer": "no tool needed — just speak the answer",
        "search": "search the web for current information",
        "pass": "none of the above — needs the full assistant",
    },
}

_ENTITY_STATE = (
    "Voice assistant Ada will act on one smart-home device for this "
    "user command. Command: \"{turn}\"")


def _entity_question(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    criteria = {
        "none": "no listed entity matches the command",
    }
    for ent in candidates:
        label = ent.get("entity_id") or ""
        if not label or label == "none":
            continue
        criteria[label] = (
            f"{ent.get('name') or label} "
            f"({ent.get('domain') or '?'}, {ent.get('state') or '?'})")
    return {
        "type": "choice",
        "instructions": (
            "Pick the entity_id of the single device the command means, "
            "or 'none' when no listed entity fits."),
        "criteria": criteria,
    }


# --- deterministic helpers (L0) -------------------------------------------

_WORD_RE = re.compile(r"\w", re.UNICODE)

# Direction verbs — extracted deterministically; the local model only
# routes and picks the entity. State checks are listed FIRST: "is the
# fan on" contains 'on' but is a read, not an actuation.
_STATE_CHECK_RE = re.compile(
    r"\b(is|are)\b.{0,30}\b(on|off|open|closed|playing|paused|"
    r"running|locked|muted)\b\s*[?？.]?\s*$|"
    r"\bstatus of\b|\bcheck\b.{0,20}\b(on|off|open|closed)\b|"
    r"เปิดอยู่|ปิดอยู่|ติดอยู่|ดับอยู่",
    re.IGNORECASE)
_OFF_RE = re.compile(
    r"\b(turn|switch|shut|power|put)\s+\w*\s*off\b|\boff\b|"
    r"\bkill\b|\bcut\b|"
    r"ปิด|ดับ",
    re.IGNORECASE)
_ON_RE = re.compile(
    r"\b(turn|switch|power|put)\s+\w*\s+on\b|\bon\b|"
    r"\bstart\b|\bfire\s+up\b|"
    r"เปิด|ติด",
    re.IGNORECASE)
# Direction vocabulary: play|pause|stop|mute|vol_up|vol_down are media
# verbs; open|close are cover verbs. 'stop' is shared — the picked
# domain translates it (_action_for) rather than guessing here.
_VERB_MAP = (
    (re.compile(r"\bpause\b|พัก", re.IGNORECASE), "pause"),
    (re.compile(r"\bresume\b|\bplay\b|เล่น", re.IGNORECASE), "play"),
    (re.compile(r"\bmute\b|ปิดเสียง", re.IGNORECASE), "mute"),
    (re.compile(r"\bvolume\s*up\b|\blouder\b|เสียง\s*ดัง|ดังขึ้น",
                re.IGNORECASE), "vol_up"),
    (re.compile(r"\bvolume\s*down\b|\bquieter\b|เบา\s*เสียง|เบาลง",
                re.IGNORECASE), "vol_down"),
    (re.compile(r"\bstop\b|หยุด", re.IGNORECASE), "stop"),
    (re.compile(r"\bopen\b|เปิด", re.IGNORECASE), "open"),
    (re.compile(r"\bclose\b|ปิด", re.IGNORECASE), "close"),
)
_TIME_RE = re.compile(
    r"\bwhat('s| is| was)?\s+the\s+(time|date|day)\b|\bwhat\s+(time|day|"
    r"date)\s+is\s+(it|today)\b|\btime\s+is\s+it\b|\btime\s+now\b|"
    r"กี่โมง|วันอะไร|วันที่\s*เท่าไหร่|วันที่เท่าไหร่",
    re.IGNORECASE)

# control_entity's domain contract: media_player/cover/button take
# action=, switchable domains take on=/action=on|off. Direction verbs
# translate per picked domain; anything unmappable returns None so the
# turn falls through to the normal dialog.
_ACTION_FOR = {
    "media_player": {"on": "turn_on", "off": "turn_off",
                     "play": "media_play", "pause": "media_pause",
                     "stop": "media_stop", "mute": "volume_mute",
                     "vol_up": "volume_up", "vol_down": "volume_down"},
    "cover": {"open": "open", "close": "close", "stop": "stop",
              "on": "open", "off": "close"},
}
_BUTTON_DOMAINS = {"button", "input_button"}


def _action_for(domain: str, direction: str) -> str | None:
    if domain in _BUTTON_DOMAINS:
        return "press"
    table = _ACTION_FOR.get(domain)
    if table is not None:
        return table.get(direction)
    # Switchable domains: on/off pass through; 'stop' reads as off
    # ("stop the fan"); media/cover-only verbs don't map.
    return {"on": "on", "off": "off", "stop": "off"}.get(direction)


def is_candidate(text: str) -> bool:
    """Short enough to be a command — word cap for segmented languages;
    unsegmented Thai is a handful of spaceless 'words', so it passes on
    a short char count instead."""
    text = (text or "").strip()
    if not text or not _WORD_RE.search(text):
        return False
    words = len(text.split())
    if words > max_words():
        return False
    # A lone unsegmented token is only a candidate when short — Thai
    # commands tokenize to one 'word', so the word cap alone can't see
    # a 60-char Thai sentence as long.
    return words >= 2 or len(text) <= max_words() * 3


def _direction(text: str) -> str | None:
    """Deterministic direction/verb for a control command.

    Returns 'state' for state checks, 'on'/'off' for plain power, an HA
    media/cover action verb, or None when nothing matches (ambiguous —
    the lane must not guess an actuation)."""
    if _STATE_CHECK_RE.search(text):
        return "state"
    # Power verbs first — Thai ปิด means both 'off' and 'close'; the
    # picked domain translates off→close for covers, so resolving power
    # intent here keeps Thai commands on the right seat.
    off, on = bool(_OFF_RE.search(text)), bool(_ON_RE.search(text))
    if off and not on:
        return "off"
    if on and not off:
        return "on"
    for rx, verb in _VERB_MAP:
        if rx.search(text):
            return verb
    return None


def _time_reply(text: str) -> str | None:
    """Deterministic time/date answer — zero model call, zero tools."""
    if not _TIME_RE.search(text or ""):
        return None
    now = time.localtime()
    if re.search(r"\b(date|day)\b|วันที่|วันอะไร", text, re.IGNORECASE):
        return f"It's {time.strftime('%A, %B %-d, %Y', now)}."
    return f"It's {time.strftime('%-I:%M %p', now)}."


def _entity_candidates(text: str, entities: list[dict[str, Any]],
                       limit: int = _ENTITY_PICK_LIMIT) -> list[dict[str, Any]]:
    """Token-overlap prefilter before the model pick — keeps the choice
    criteria small and keeps 'none' honest when nothing matches."""
    tokens = {t for t in re.split(r"[^\w]+", (text or "").lower()) if t}
    scored = []
    for ent in entities:
        hay = f"{ent.get('name', '')} {ent.get('entity_id', '')}".lower()
        score = sum(2 if t in hay else 0 for t in tokens)
        score += sum(1 for t in tokens if t and t in ent.get("entity_id", "").lower())
        if score:
            scored.append((score, ent))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("name", "").lower()))
    return [ent for _, ent in scored[:limit]]


# --- config ---------------------------------------------------------------

def mode() -> str:
    return os.environ.get(_MODE_ENV, "shadow").strip().lower()


def llm_url() -> str | None:
    url = (os.environ.get(_URL_ENV)
           or os.environ.get("ADA_JEV_URL") or "").strip()
    return url.rstrip("/") or None


def max_words() -> int:
    try:
        return int(os.environ.get(_MAX_WORDS_ENV, _DEFAULT_MAX_WORDS))
    except ValueError:
        return _DEFAULT_MAX_WORDS


def min_conf() -> float:
    try:
        return float(os.environ.get(_MIN_CONF_ENV, _DEFAULT_MIN_CONF))
    except ValueError:
        return _DEFAULT_MIN_CONF


def timeout_s() -> float:
    try:
        return float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def corpus_path() -> Path:
    return Path(os.environ.get(
        _CORPUS_ENV, os.path.expanduser(
            "~/.local/share/ada/command-lane-corpus.jsonl")))


def armed() -> bool:
    """Shadow or enforce — the lane runs model calls either way."""
    return mode() in ("shadow", "enforce") and llm_url() is not None


def enforced() -> bool:
    """Only enforce mode lets the lane actually answer a turn."""
    return mode() == "enforce" and llm_url() is not None


# --- model calls -----------------------------------------------------------

def _systemone(url: str, state: str, question: dict,
               timeout: float) -> dict | None:
    """One POST to /v1/systemone. Sync — callers wrap in to_thread."""
    payload = {"state": state, "questions": {"q": question}}
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/systemone",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        return (resp.get("answers") or {}).get("q") or None
    except Exception as exc:
        logger.info("command lane systemone call failed: %s", exc)
        return None


def classify(text: str, *, url: str | None = None,
             timeout: float | None = None) -> dict | None:
    """Route question → {"action", "confidence"} or None on failure."""
    url = url or llm_url()
    if not url:
        return None
    ans = _systemone(url, _ROUTE_STATE.format(turn=text[:300]),
                     _ROUTE_QUESTION, timeout or timeout_s())
    if not ans:
        return None
    try:
        return {"action": str(ans.get("choice") or ""),
                "confidence": float(ans.get("confidence") or 0)}
    except (TypeError, ValueError):
        return None


def pick_entity(text: str, entities: list[dict[str, Any]],
                *, url: str | None = None,
                timeout: float | None = None) -> dict | None:
    """Second hop — choice over the real controllable set.

    Returns {"entity_id", "confidence"} or None (model said 'none',
    call failed, or no candidates)."""
    url = url or llm_url()
    candidates = _entity_candidates(text, entities)
    if not url or not candidates:
        return None
    ans = _systemone(url, _ENTITY_STATE.format(turn=text[:300]),
                     _entity_question(candidates), timeout or timeout_s())
    if not ans:
        return None
    pick = str(ans.get("choice") or "")
    names = {e.get("entity_id"): e.get("name") for e in candidates}
    if not pick or pick == "none" or pick not in names:
        # A label outside the offered criteria is a hallucinated entity —
        # treat it like 'none' rather than actuate a phantom id.
        return None
    try:
        conf = float(ans.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"entity_id": pick, "name": names.get(pick, pick),
            "confidence": conf}


# --- corpus ----------------------------------------------------------------

def record(row: dict[str, Any]) -> None:
    """Append one corpus row — same JSONL convention as jev-corpus."""
    try:
        path = corpus_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.info("command lane corpus append failed: %s", exc)


def probe(text: str, *, surface: str, session_id: str = "",
          tools: list[str] | None = None,
          emit: Callable[..., None] | None = None) -> None:
    """Fire-and-forget shadow probe — schedule the classify, record the
    row, emit a command_lane ops event when the lane would have taken
    the turn. Never blocks the caller's turn."""
    if not armed() or not is_candidate(text):
        return
    text = (text or "").strip()[:300]

    async def _run() -> None:
        t0 = time.monotonic()
        route = await asyncio.to_thread(classify, text)
        ms = int((time.monotonic() - t0) * 1000)
        row = {
            "ts": time.time(), "surface": surface,
            "session": session_id, "text": text,
            "mode": mode(), "ms": ms,
            "tools": list(tools or []),
        }
        if route:
            row["route"] = route["action"]
            row["conf"] = round(route["confidence"], 4)
            row["direction"] = _direction(text)
            row["servable"] = (
                route["confidence"] >= min_conf()
                and route["action"] in _SERVABLE_ROUTES)
        record(row)
        if emit and row.get("servable"):
            emit("command_lane",
                 f"{surface}: would handle {text[:80]!r} "
                 f"route={row['route']} conf={row['conf']} "
                 f"(gemini tools: {', '.join(row['tools']) or 'none'})")

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return


# --- enforce path ------------------------------------------------------------

# Routes the lane can complete end-to-end: 'control' actuates (or reads
# state — a 'state' direction never actuates); 'answer' is servable only
# when the turn is a state check or the deterministic time/date path
# already fired. remember/recall/search/pass always fall through.
_SERVABLE_ROUTES = {"control", "answer"}


async def try_handle(text: str, runner: Any, *,
                     identity: Any = None, speaker: Any = None,
                     owner: Any = None, session: Any = None,
                     surface: str = "text") -> dict | None:
    """Enforce path: serve a short turn locally or return None so the
    caller falls through to the normal provider turn.

    Gate denials deliberately return None — a dangerous-device confirm
    or an ACL denial belongs to the normal dialog, not a flat machine
    reply."""
    if not enforced():
        return None
    text = (text or "").strip()[:300]
    row: dict[str, Any] = {
        "ts": time.time(), "surface": surface, "session": session or "",
        "text": text, "mode": mode(),
    }
    try:
        reply = _time_reply(text)
        if reply:
            row.update(route="time", handled=True)
            return {"reply": reply, "route": "time", "tool": None}
        if not is_candidate(text):
            return None
        t0 = time.monotonic()
        route = await asyncio.to_thread(classify, text)
        row["ms"] = int((time.monotonic() - t0) * 1000)
        if not route:
            return None
        row["route"], row["conf"] = route["action"], round(
            route["confidence"], 4)
        row["direction"] = _direction(text)
        if (route["confidence"] < min_conf()
                or route["action"] not in _SERVABLE_ROUTES):
            return None
        direction = row["direction"]
        if route["action"] == "answer" and direction != "state":
            # The lane can't generate free answers — only the
            # deterministic time path and state reads are servable.
            return None
        if direction is None:
            # Ambiguous actuation ("the fan, please") — never guess.
            return None
        ha = getattr(getattr(runner, "context", None), "ha_client", None)
        if ha is None:
            return None
        try:
            entities = await ha.entities()
        except Exception as exc:
            logger.info("command lane entity list failed: %s", exc)
            return None
        pick = await asyncio.to_thread(pick_entity, text, entities)
        if not pick or pick["confidence"] < min_conf():
            row["pick"] = (pick or {}).get("entity_id")
            return None
        row["pick"], row["pick_conf"] = (
            pick["entity_id"], round(pick["confidence"], 4))
        if direction == "state":
            args = {"entity_id": pick["entity_id"]}
            out = await runner.execute(
                "get_home_state", args, identity=identity,
                speaker=speaker, owner=owner, session=session)
            row.update(handled=True, tool="get_home_state", args=args)
            return {
                "reply": _state_reply(pick["name"], out),
                "route": "control", "tool": "get_home_state",
                "args": args, "result": out}
        domain = pick["entity_id"].partition(".")[0]
        action = _action_for(domain, direction)
        if action is None:
            # Direction makes no sense for the picked domain
            # ("play the kettle") — never guess an actuation.
            row["unmapped_direction"] = direction
            return None
        args = {"entity_id": pick["entity_id"]}
        if domain in _ACTION_FOR or domain in _BUTTON_DOMAINS:
            args["action"] = action
        else:
            args["on"] = action == "on"
        out = await runner.execute(
            "control_entity", args, identity=identity,
            speaker=speaker, owner=owner, session=session)
        row.update(handled=True, tool="control_entity", args=args)
        return {
            "reply": _control_reply(pick["name"], direction, out),
            "route": "control", "tool": "control_entity",
            "args": args, "result": out}
    except Exception as exc:
        # Gate denials (PermissionError) and runner failures fall
        # through to the normal provider turn — the lane never eats an
        # exception the dialog would have surfaced.
        row["error"] = str(exc)[:200]
        logger.info("command lane fell through: %s", exc)
        return None
    finally:
        record(row)


def _state_reply(name: str, out: Any) -> str:
    state = None
    if isinstance(out, dict):
        state = out.get("state") or out.get("output")
        if isinstance(state, dict):
            state = state.get("state")
    if state is None and isinstance(out, str):
        state = out
    label = str(name or "the device")
    if not state:
        return f"Couldn't read {label}."
    return f"{label} is {state}."


def _control_reply(name: str, direction: str, out: Any) -> str:
    label = str(name or "the device")
    if isinstance(out, dict) and out.get("ok") is False:
        err = str(out.get("error") or "the device did not respond")
        return f"Tried, but {err.split('—')[0].strip()}"
    verbs = {
        "on": "turned on", "off": "turned off",
        "pause": "paused", "play": "playing", "stop": "stopped",
        "mute": "muted", "vol_up": "volume up on",
        "vol_down": "volume down on",
        "open": "opened", "close": "closed",
    }
    return f"Done — {verbs.get(direction, direction)} {label}."


def reset() -> None:
    """Test hook — no module state yet, kept for parity with pool modules."""
