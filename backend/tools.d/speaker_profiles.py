"""speaker_profiles — voiceprint store admin (card speaker-profile-tool).

Backed by backend/speaker_id.py (the ECAPA voiceprint store, file-backed
at ~/.local/share/ada-pi/speaker_profiles.json — never a memory bank, so
ada_forget redirects here).

Actions (manifest policy 'read' can't vary per action — mutations are
gated in-module by _may_mutate):
  list      — enrolled profiles: name, ha_person, display_name, samples,
              media flag, aliases. Free for any session.
  match     — score the session's buffered voice against every profile:
              best name + score even below threshold, so Ada can say
              "you're enrolled as พรศิริ — merge into KK?" instead of
              looping on the ada_enroll_speaker refusal.
  remove    — delete a profile (name= accepts profile/alias/person).
  re-enroll — force-replace a profile's print with the buffered voice.
  alias     — bind a profile to an HA person (person='kk'/'person.kk')
              so both names resolve to one identity; optional
              display_name= and rename_to= (profile key rename keeps the
              old name resolving via aliases).

Mutation rule: full-access identities (owner) always; a non-owner may
only touch the profile bound to their own person.* (self-service fix —
KK repairing her own misnamed print). Secondary turns never mutate.
"""

from __future__ import annotations

import re
from typing import Any

DECLARATION = {
    "name": "speaker_profiles",
    "description": (
        "Enrolled voiceprint admin — action='list'|'match'|'remove'|"
        "'re-enroll'|'alias'. 'match' names which profile the current "
        "voice scores as; 'alias' binds a profile to person.* "
        "(rename_to= merges a wrong-named print)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'list' (default), 'match', 'remove', "
                               "'re-enroll', or 'alias'.",
            },
            "name": {
                "type": "string",
                "description": "Target profile — accepts the enrolled "
                               "name, an alias, or 'person.x'.",
            },
            "person": {
                "type": "string",
                "description": "HA person to bind for 'alias'/'re-enroll' "
                               "— 'KK' or 'person.kk'.",
            },
            "display_name": {
                "type": "string",
                "description": "Friendly name for 'alias'/'re-enroll'.",
            },
            "rename_to": {
                "type": "string",
                "description": "New profile key for 'alias' — merges a "
                               "wrong-named profile ('พรศิริ' -> 'KK'); "
                               "the old name keeps resolving as an alias.",
            },
            "seconds": {
                "type": "number",
                "description": "Audio buffer window for 'match'/"
                               "'re-enroll' (default 15s).",
            },
        },
        "required": [],
    },
}

_MUTATING = {"remove", "re-enroll", "reenroll", "alias"}
_PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")


def _identifier() -> Any:
    from backend.speaker_id import SpeakerIdentifier
    return SpeakerIdentifier.get()


def _speaker_session(runner: Any) -> Any:
    """The calling session's SpeakerSession — the provider passes it per
    execute() call; an explicit None means THIS session has no voice
    buffer (text channel, speaker ID off) and must not fall back to
    another session's live buffer via the shared runner field. The shared
    field is only for non-ws callers (REST/tests). Same lookup
    ada_enroll_speaker uses."""
    try:
        from backend.tool_runner.common import (
            _CALLER_SPEAKER_SESSION, _IDENTITY_UNSET)
        sess = _CALLER_SPEAKER_SESSION.get()
        if sess is not _IDENTITY_UNSET:
            return sess
    except Exception:
        pass
    return getattr(runner, "speaker_session", None)


def _may_mutate(runner: Any, target_person: str | None) -> str | None:
    """None when the caller may mutate voiceprints, else the refusal
    reason (spoken-aloud safe). Owners = 'admin' identity or a {full:
    true} person/control policy — same convention as _persona_admin;
    self-service = the profile is bound to the caller's own person.*."""
    try:
        if runner._is_secondary_turn():
            return ("the identified speaker isn't the session owner — "
                    "voiceprint changes need the owner, or a session "
                    "keyed to the speaker's own person")
    except Exception:
        pass
    ident = None
    try:
        ident = runner.policy_identity()
    except Exception:
        pass
    if ident == "admin":
        return None
    try:
        banks = runner.banks
        for policies in (banks.person_policies, banks.control_policies):
            if (policies.get(ident) or {}).get("full"):
                return None
    except Exception:
        pass
    if target_person and ident and target_person == ident:
        return None
    return ("voiceprint changes need a full-access identity — ask the "
            "owner to run it, or use a session keyed to your own person")


def _list(ident: Any) -> dict[str, Any]:
    profiles = ident.enrolled_info()
    return {"ok": True, "count": len(profiles), "profiles": profiles}


def _match(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    sess = _speaker_session(runner)
    if sess is None:
        return {"ok": False, "error": (
            "speaker identification is not active on this session — "
            "there is no voice buffer to score (speaker ID may be "
            "disabled or this is not a voice session)")}
    try:
        seconds = float(args.get("seconds") or 15.0)
    except (TypeError, ValueError):
        seconds = 15.0
    try:
        out = sess.match_buffer(seconds)
    except Exception as exc:
        return {"ok": False, "error": f"could not score the buffer: {exc}"}
    out["ok"] = True
    if out.get("match"):
        out["say"] = (
            f"the buffered voice matches '{out['match']}' "
            f"({out['score']:.0%}) — tell the speaker they're enrolled "
            "under that name and offer to merge/rename it")
    elif out.get("best"):
        out["say"] = (
            f"best candidate is '{out['best']}' at {out['score']:.0%} "
            f"(below the {out['threshold']:.2f} threshold — treat as a "
            "hint, not a confirmed identity)")
    return out


def _seconds(args: dict[str, Any]) -> float:
    try:
        return float(args.get("seconds") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def _resolve_or_err(ident: Any, name: Any) -> tuple[str | None, dict | None]:
    target = ident.resolve_name(str(name or "").strip())
    if target is None:
        known = ", ".join(
            p["name"] for p in ident.enrolled_info()) or "none"
        return None, {"ok": False, "error": (
            f"no enrolled speaker profile matches {name!r} "
            f"(enrolled: {known})")}
    return target, None


async def _remove(runner: Any, ident: Any, args: dict[str, Any]) -> dict[str, Any]:
    target, err = _resolve_or_err(ident, args.get("name"))
    if err:
        return err
    deny = _may_mutate(runner, ident.get_ha_person(target))
    if deny:
        return {"ok": False, "error": deny}
    if ident.remove(target):
        _log("speaker-profile-removed", target, runner)
        return {"ok": True, "removed": target,
                "note": "the voiceprint is gone — the speaker will be "
                        "unrecognized until they re-enroll"}
    return {"ok": False, "error": f"remove failed for '{target}'"}


async def _reenroll(runner: Any, ident: Any, args: dict[str, Any]) -> dict[str, Any]:
    sess = _speaker_session(runner)
    if sess is None:
        return {"ok": False, "error": (
            "speaker identification is not active on this session — "
            "no voice buffer to re-enroll from")}
    target, err = _resolve_or_err(ident, args.get("name"))
    if err:
        return err
    person = str(args.get("person") or "").strip()
    ha_person = None
    if person:
        if _PERSON_RE.match(person):
            ha_person = person
        else:
            resolved = await _resolve_person(runner, person)
            if not resolved or not resolved.get("entity_id"):
                return {"ok": False, "error": (
                    f"no Home Assistant person matches {person!r} — "
                    "pass person=<name> or a person.<id> entity")}
            ha_person = resolved["entity_id"]
    deny = _may_mutate(runner, ha_person or ident.get_ha_person(target))
    if deny:
        return {"ok": False, "error": deny}
    try:
        # force=True replaces the print outright — re-enroll is the
        # recovery path for a stale/wrong-named profile, not a merge.
        result = sess.enroll_from_buffer(
            target, ha_person=ha_person,
            display_name=str(args.get("display_name") or "").strip() or None,
            seconds=_seconds(args), force=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "matched_profile": getattr(exc, "matched", None)}
    _log("speaker-profile-reenrolled", result.get("name") or target, runner)
    return {"ok": True, "reenrolled": result.get("name"),
            "samples": result.get("samples"),
            "ha_person": result.get("ha_person"),
            "note": "profile replaced with the buffered voice — earlier "
                    "prints under this name are gone"}


async def _alias(runner: Any, ident: Any, args: dict[str, Any]) -> dict[str, Any]:
    target, err = _resolve_or_err(ident, args.get("name"))
    if err:
        return err
    person_arg = str(args.get("person") or "").strip()
    ha_person = None
    friendly = None
    if person_arg:
        if _PERSON_RE.match(person_arg):
            ha_person = person_arg
        else:
            resolved = await _resolve_person(runner, person_arg)
            if not resolved or not resolved.get("entity_id"):
                return {"ok": False, "error": (
                    f"no Home Assistant person matches {person_arg!r} — "
                    "pass person=<name> or a person.<id> entity")}
            ha_person, friendly = resolved["entity_id"], resolved.get("name")
    display = str(args.get("display_name") or "").strip() or friendly or None
    existing_person = ident.get_ha_person(target)
    deny = _may_mutate(runner, ha_person or existing_person)
    if deny:
        return {"ok": False, "error": deny}
    # A bound profile is already claimed — a non-owner may only rebind
    # within their own person entity (self-service), never steal it.
    if existing_person and ha_person and existing_person != ha_person \
            and _may_mutate(runner, existing_person) is not None:
        return {"ok": False, "error": (
            f"'{target}' is already bound to {existing_person} — "
            "rebinding it elsewhere needs a full-access identity")}
    rename_to = str(args.get("rename_to") or "").strip()
    changed = False
    def _meta(name: str) -> dict:
        return next(
            (p for p in ident.enrolled_info() if p["name"] == name), {})

    if ha_person or display:
        # Auto-seed aliases with the person's friendly name + entity slug
        # so 'KK' and 'person.kk' both resolve to this profile.
        meta_aliases = set(_meta(target).get("aliases") or [])
        for extra in (friendly, (ha_person or "").split(".", 1)[-1]):
            if extra and extra.strip():
                meta_aliases.add(extra.strip())
        if not ident.set_metadata(target, ha_person=ha_person,
                                  display_name=display,
                                  aliases=sorted(meta_aliases)):
            return {"ok": False, "error": f"could not update '{target}'"}
        changed = True
    if rename_to:
        new = ident.rename(target, rename_to)
        if new is None:
            return {"ok": False, "error": (
                f"could not rename '{target}' to {rename_to!r} — the new "
                "name is taken or a reserved placeholder")}
        target = new
        changed = True
    if not changed:
        return {"ok": False, "error": (
            "alias needs person=, display_name=, or rename_to= — nothing "
            "to change")}
    _log("speaker-profile-aliased", target, runner)
    meta = _meta(target)
    return {"ok": True, "profile": target,
            "ha_person": meta.get("ha_person"),
            "display_name": meta.get("display_name"),
            "aliases": list(meta.get("aliases") or []),
            "note": "both names now resolve to this one profile"}


async def _resolve_person(runner: Any, person: str) -> dict | None:
    try:
        return await runner.context.ha_client.resolve_person(person)
    except Exception:
        return None


def _log(kind: str, target: Any, runner: Any) -> None:
    try:
        from backend.event_log import log_event
        who = None
        try:
            who = runner.policy_identity()
        except Exception:
            pass
        log_event(kind, str(target), "voice", f"by={who or '?'}")
    except Exception:
        pass


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()
    try:
        ident = _identifier()
    except Exception as exc:
        return {"ok": False,
                "error": f"speaker identification is unavailable: {exc}"}
    if action == "list":
        return _list(ident)
    if action == "match":
        return _match(runner, args)
    if action == "remove":
        return await _remove(runner, ident, args)
    if action in ("re-enroll", "reenroll"):
        return await _reenroll(runner, ident, args)
    if action == "alias":
        return await _alias(runner, ident, args)
    return {"ok": False, "error": (
        f"unknown action {action!r} — use list, match, remove, "
        "re-enroll, or alias")}
