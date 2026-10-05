#!/usr/bin/env python3
"""kanban-autofix — the auto_fix rule hook for the kanban lifecycle.

kanban-dispatch (chaba repo) calls this when a card changes column:

    python3 scripts/ada/kanban_autofix.py --card-id cms-auto-flood-report \
        --column review

Rules map card-id prefixes to playbooks — `auto_fix: [cms-auto-*]` in the
kanban config is this table:

    cms-auto-* -> playbook cms-regen (slug = card id minus 'cms-auto-')

Only matching cards act; every other card id returns {'skipped': ...} —
the rule NEVER dispatches on non-matching cards. The hook fires on
column='review' only (pass --force to override).

Modes:
    dispatch (default) — backend.devin_dispatch playbook dispatch: a
        worktree session runs the cms-regen playbook end-to-end.
    run — execute scripts/ada/cms_regen.py in-process. Same regen
        contract, no session; for smoke tests and quick manual sweeps.

Results land on the card's comms either way (the dispatched session posts
via cms_regen --card; run mode posts inline). A comms line containing
'cms-regen' inside --dedup-min suppresses a repeat fire so a card that
re-enters review in a loop doesn't spawn a session every pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Callable

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))

BOARD_API_URL = os.environ.get("ADA_BOARD_API_URL", "http://127.0.0.1:8787")
DEFAULT_REPO = os.environ.get("KANBAN_AUTOFIX_REPO", "ada-pi")

# auto_fix rules: card-id prefix -> playbook name. Glob-style 'cms-auto-*'
# entries are accepted and normalized to the prefix.
AUTO_FIX_RULES = {"cms-auto-": "cms-regen"}

# Only fire on entering this column.
TRIGGER_COLUMN = "review"

# Skip when a comms line already mentions the playbook inside this
# window — re-entrant review transitions must not spawn a session per
# pass.
DEDUP_MIN = 30

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COMMS_TS = "%Y-%m-%d %H:%M"


def _post_json(url: str, payload: dict | None = None,
               headers: dict | None = None,
               timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except Exception:
            data = {}
        return exc.code, data if isinstance(data, dict) else {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def _get_json(url: str, headers: dict | None = None,
              timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except Exception:
        return 0, {}


def parse_rules(spec: str | None) -> dict[str, str]:
    """'cms-auto-*:cms-regen[,other-*:other-playbook]' -> {prefix: playbook}.
    None/empty -> the built-in AUTO_FIX_RULES."""
    if not spec or not spec.strip():
        return dict(AUTO_FIX_RULES)
    rules: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        pat, _, playbook = item.partition(":")
        prefix = pat.strip().rstrip("*")
        playbook = playbook.strip() or "cms-regen"
        if prefix:
            rules[prefix] = playbook
    return rules


def match(card_id: str, rules: dict[str, str] | None = None
          ) -> tuple[str, str, str] | None:
    """card_id -> (prefix, playbook, slug) or None. A prefix whose
    remainder isn't a valid slug never fires."""
    cid = str(card_id or "").strip()
    for prefix, playbook in (rules or AUTO_FIX_RULES).items():
        if cid.startswith(prefix):
            slug = cid[len(prefix):]
            if _SLUG_RE.match(slug):
                return prefix, playbook, slug
    return None


def _find_card(board: str, card_id: str, get_json) -> dict | None:
    status, data = get_json(f"{board}/cards")
    if not (200 <= status < 300):
        return None
    for c in (data or {}).get("cards") or []:
        if c.get("id") == card_id:
            return c
    return None


def _recent_fix(card: dict | None, within_min: float,
                playbook: str, now: datetime | None = None) -> bool:
    """True when the card's comms already show an autofix/playbook line
    younger than within_min."""
    if not card or within_min <= 0:
        return False
    now = now or datetime.now()
    for c in reversed(card.get("comms") or []):
        text = str(c.get("text") or "")
        if playbook not in text and "auto_fix" not in text:
            continue
        try:
            at = datetime.strptime(str(c.get("at") or "")[:16], _COMMS_TS)
        except ValueError:
            continue
        if (now - at).total_seconds() <= within_min * 60:
            return True
    return False


def _board_comment(board: str, card_id: str, text: str,
                   post_json, actor: str = "devin") -> bool:
    status, data = post_json(
        f"{board}/comment",
        {"id": card_id, "from": actor, "text": text[:480]})
    return 200 <= status < 300 and bool((data or {}).get("ok", True))


def _default_dispatch(repo: str, playbook: str,
                      params: dict[str, Any]) -> dict[str, Any]:
    """Real playbook dispatch through backend.devin_dispatch — the same
    path the `devin` tool uses, minus the voice confirm (kanban-dispatch
    is the owner's automation, so the playbook's owner gate is satisfied
    by the host identity, not a speaker label)."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from backend import devin_dispatch
    try:
        from backend.mddb_client import MddbClient
        mddb = MddbClient()
    except Exception:
        mddb = None
    return asyncio.run(devin_dispatch.dispatch(
        repo, playbook=playbook, params=params,
        mddb=mddb, caller="kanban-autofix", is_owner=True))


def _default_run(slug: str, card_id: str | None,
                 board_url: str) -> dict[str, Any]:
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    import cms_regen
    return cms_regen.run(slug, card_id=card_id, board_url=board_url)


def handle_card(card_id: str, column: str = TRIGGER_COLUMN, *,
                mode: str = "dispatch",
                rules: dict[str, str] | None = None,
                repo: str = DEFAULT_REPO,
                board_url: str = BOARD_API_URL,
                dedup_min: float = DEDUP_MIN,
                force: bool = False,
                dry_run: bool = False,
                comms_from: str = "devin",
                dispatch_fn: Callable | None = None,
                run_fn: Callable | None = None,
                post_json=_post_json,
                get_json=_get_json) -> dict[str, Any]:
    """The auto_fix rule. Only cards matching a prefix rule on the trigger
    column ever act — everything else is a reported no-op."""
    card_id = str(card_id or "").strip()
    out: dict[str, Any] = {"ok": True, "card_id": card_id}
    if str(column or "").strip().lower() != TRIGGER_COLUMN and not force:
        out["skipped"] = f"column {column!r} != {TRIGGER_COLUMN!r}"
        return out
    m = match(card_id, rules)
    if not m:
        out["skipped"] = "no auto_fix rule"
        return out
    prefix, playbook, slug = m
    out.update(slug=slug, playbook=playbook, mode=mode)

    if not force and dedup_min > 0:
        card = _find_card(board_url, card_id, get_json)
        if _recent_fix(card, dedup_min, playbook):
            out["skipped"] = f"recent {playbook} comms within {dedup_min}min"
            return out
    if dry_run:
        out["dry_run"] = True
        return out

    if mode == "dispatch":
        params = {"slug": slug, "card": card_id}
        try:
            res = (dispatch_fn or _default_dispatch)(repo, playbook, params)
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out["result"] = res
        task_id = res.get("task_id") if isinstance(res, dict) else None
        out["ok"] = bool(task_id)
        _board_comment(
            board_url, card_id,
            (f"auto_fix {playbook}: dispatched for '{slug}' — task "
             f"{task_id}" if task_id else
             f"auto_fix {playbook}: dispatch failed for '{slug}' — "
             f"{(res or {}).get('error', 'unknown')}"),
            post_json, actor=comms_from)
    else:  # run — in-process, cms_regen posts the outcome to comms itself
        res = (run_fn or _default_run)(slug, card_id, board_url)
        out["result"] = res
        out["ok"] = bool((res or {}).get("ok"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="auto_fix hook — dispatch a playbook when a matching "
                    "card enters review (cms-auto-* -> cms-regen).")
    ap.add_argument("--card-id", required=True)
    ap.add_argument("--column", default=TRIGGER_COLUMN,
                    help=f"column the card entered (default {TRIGGER_COLUMN})")
    ap.add_argument("--mode", choices=("dispatch", "run"),
                    default="dispatch")
    ap.add_argument("--rules",
                    help="'prefix:playbook[,...]' — default "
                         "cms-auto-*:cms-regen")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="dispatch repo (default ada-pi)")
    ap.add_argument("--board", default=BOARD_API_URL)
    ap.add_argument("--dedup-min", type=float, default=DEDUP_MIN)
    ap.add_argument("--from", dest="actor", default="devin",
                    help="comms actor — board whitelist ada/chaba/devin/tony")
    ap.add_argument("--force", action="store_true",
                    help="fire regardless of column/dedup")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = handle_card(
        a.card_id, a.column, mode=a.mode, rules=parse_rules(a.rules),
        repo=a.repo, board_url=a.board, dedup_min=a.dedup_min,
        force=a.force, dry_run=a.dry_run, comms_from=a.actor)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(json.dumps(out, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
