#!/usr/bin/env python3
"""cms-regen — the act step for cms-auto-* kanban cards.

cms-auto-health (chaba repo) flags stale ada-cms-automation registry docs
as cms-auto-<slug> cards; this closes the loop. Given a slug:

  1. POST /api/cms/pages/<slug>/regenerate on the ada CMS API —
     command generators run synchronously, everything else queues
     run_now for the scheduled worker.
  2. Poll the registry doc until last_run advances (or run_now is
     consumed) or a bounded timeout expires.
  3. Re-check health: registry last_status, page `updated` freshness,
     and the /verify parse check.
  4. Phantom-registry handling: the doc was never consumed AND the page
     itself is fresh — the page is really refreshed by a bulk timer
     (the camwall-noble-a pattern, cam-wall-cms.timer). Write
     interval_min=0 + managed_by=<owner> so the health check stops
     re-flagging it. Pages that are stale too stay 'stalled' — those
     escalate, not get marked.
  5. Post the outcome to the card comms (--card) and stamp the job
     ledger (ada-ha-bank-devin-handoff, kind=job).

Registry contract (tool_runner.cms_automation): one doc per slug in
ada-cms-automation, contentMd = JSON {enabled, interval_min, run_now,
generator?, feeds?, last_run/last_status/last_error (worker write-back)}.

Env: ADA_CMS_API_URL (CMS API), ADA_API_KEY (x-api-key), MDDB_BASE_URL
(registry + ledger), ADA_BOARD_API_URL (card comms). Stdlib-only so it
runs on any host with a checkout — no venv needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

MDDB_BASE_URL = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")
CMS_API_URL = os.environ.get("ADA_CMS_API_URL", "http://127.0.0.1:8080")
BOARD_API_URL = os.environ.get("ADA_BOARD_API_URL", "http://127.0.0.1:8787")
AUTOMATION_COLLECTION = os.environ.get(
    "ADA_CMS_AUTOMATION_COLLECTION", "ada-cms-automation")
CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
HANDOFF_COLLECTION = os.environ.get(
    "ADA_DEVIN_HANDOFF_COLLECTION", "ada-ha-bank-devin-handoff")

# Same shape as tool_runner._SLUG_RE — registry/page keys are page slugs.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEFAULT_TIMEOUT_S = 300
DEFAULT_POLL_S = 10
# A page whose `updated` is younger than this is being refreshed by
# something — combined with an unconsumed run_now it proves the bulk-timer
# (phantom) case. 26h covers daily sweeps; longer if the doc's own
# interval_min implies a slower cadence (see _fresh_window).
DEFAULT_FRESH_WITHIN_S = 26 * 3600
# Command generators run synchronously inside the POST; the write-back to
# the registry doc lands after. Bound that tail separately from the
# queued-run poll.
GENERATOR_WRITEBACK_S = 60

PostJson = Callable[..., tuple[int, dict[str, Any]]]


# -- transports (patch points for tests) -------------------------------------

def _post_json(url: str, payload: dict | None = None,
               headers: dict | None = None,
               timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
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
    req = urllib.request.Request(url, headers=headers or {})
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


# -- registry / page / board helpers ------------------------------------------

def _cfg_of(doc: dict | None) -> dict[str, Any]:
    if not doc:
        return {}
    try:
        cfg = json.loads(doc.get("contentMd") or "{}")
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def registry_get(mddb: str, slug: str,
                 post_json: PostJson) -> tuple[int, dict | None, dict]:
    """(http_status, doc, cfg) — status 0 means the store is unreachable,
    which callers must NOT confuse with 'doc absent'."""
    status, doc = post_json(
        f"{mddb}/get",
        {"collection": AUTOMATION_COLLECTION, "key": slug, "lang": "en"})
    if status >= 400 or not isinstance(doc, dict) or not doc.get("key"):
        return status, None, {}
    return status, doc, _cfg_of(doc)


def registry_save(mddb: str, slug: str, cfg: dict,
                  post_json: PostJson, now: datetime) -> bool:
    """Write-back — same meta shape as tool_runner._cms_automation_save."""
    status, _ = post_json(
        f"{mddb}/add",
        {"collection": AUTOMATION_COLLECTION, "key": slug, "lang": "en",
         "contentMd": json.dumps(cfg, ensure_ascii=False, indent=2),
         "meta": {
            "kind": ["automation-config"], "bank": ["cms"],
            "scope": ["tony"], "status": ["active"], "source": ["api"],
            "written_by": ["ada:cms-regen"], "subject": [slug],
            "attribute": ["automation"], "slug": [slug],
            "title": [f"CMS automation: {slug}"], "format": ["json"],
            "lang": ["en"], "updated": [now.isoformat(timespec="seconds")],
            "last_verified": [now.date().isoformat()]}})
    return 200 <= status < 300


def page_get(mddb: str, slug: str, post_json: PostJson) -> dict | None:
    status, doc = post_json(
        f"{mddb}/get",
        {"collection": CMS_COLLECTION, "key": slug, "lang": "en"})
    if status >= 400 or not isinstance(doc, dict) or not doc.get("key"):
        return None
    return doc


def _meta_first(doc: dict | None, name: str) -> str | None:
    v = ((doc or {}).get("meta") or {}).get(name)
    if isinstance(v, list):
        return str(v[0]) if v else None
    return str(v) if v is not None else None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_fresh(updated: str | None, window_s: float, now: datetime) -> bool:
    ts = _parse_ts(updated)
    return bool(ts and (now - ts).total_seconds() <= window_s)


def _fresh_window(cfg: dict, fresh_within_s: float) -> float:
    """A page on a 6h interval is 'fresh' much longer than a 15m one —
    honor the doc's own cadence when it exceeds the default window."""
    try:
        interval = int(cfg.get("interval_min") or 0)
    except (TypeError, ValueError):
        interval = 0
    return max(fresh_within_s, 2 * interval * 60)


def post_regenerate(api: str, slug: str, headers: dict,
                    post_json: PostJson, timeout: float) -> tuple[int, dict]:
    return post_json(
        f"{api}/api/cms/pages/{slug}/regenerate", {}, headers,
        timeout=timeout)


def verify_page(api: str, slug: str, headers: dict,
                get_json) -> dict | None:
    status, report = get_json(
        f"{api}/api/cms/pages/{slug}/verify", headers)
    return report if 200 <= status < 300 else None


def board_comment(board: str, card_id: str, text: str,
                  post_json: PostJson, actor: str = "devin") -> bool:
    status, data = post_json(
        f"{board}/comment",
        {"id": card_id, "from": actor, "text": text[:480]})
    return 200 <= status < 300 and bool((data or {}).get("ok", True))


def write_ledger(mddb: str, collection: str, slug: str,
                 out: dict, post_json: PostJson, now: datetime,
                 card_id: str | None = None) -> str | None:
    """job/<id> doc in the devin-handoff bank — the job ledger the report
    feeds read. Never raises: a down bank must not mask the real result."""
    jid = f"cms-regen-{slug}-{now:%Y%m%d-%H%M%S}"
    meta = {
        "kind": ["job"],
        "status": ["done" if out.get("ok") else "failed"],
        "job_id": [jid], "playbook": ["cms-regen"],
        "result": [str(out.get("outcome") or "error")],
        "subject": [slug], "slug": [slug], "source": ["cms-regen"],
        "written_by": ["ada:cms-regen"], "scope": ["tony"],
        "bank": ["devin-handoff"], "report_to": [collection],
        "ts": [now.isoformat(timespec="seconds")],
    }
    if card_id:
        meta["card"] = [card_id]
    body = (f"cms-regen {slug}: {out.get('outcome')}.\n\n"
            f"last_run {out.get('last_run_before')} -> "
            f"{out.get('last_run_after')}; last_status="
            f"{(out.get('health') or {}).get('last_status')}; "
            f"page updated {out.get('page_updated')}.\n"
            f"{out.get('detail') or ''}").strip()
    status, _ = post_json(
        f"{mddb}/add",
        {"collection": collection, "key": f"job/{jid}", "lang": "en",
         "contentMd": body, "meta": meta})
    return f"job/{jid}" if 200 <= status < 300 else None


def _mark_managed(cfg: dict, owner: str) -> dict:
    """Phantom-registry mark: interval_min=0 + managed_by=<owner>, and
    clear run_now — nothing is consuming the queue, so leaving it set is
    a stale flag, not a pending request. Only these keys change; any other
    registry config is a human decision (card contract)."""
    changes: dict[str, Any] = {}
    if int(cfg.get("interval_min") or 0) != 0:
        cfg["interval_min"] = 0
        changes["interval_min"] = 0
    if cfg.get("managed_by") != owner:
        cfg["managed_by"] = owner
        changes["managed_by"] = owner
    if cfg.get("run_now"):
        cfg["run_now"] = False
        changes["run_now"] = False
    return changes


def _comms_line(out: dict) -> str:
    slug = out["slug"]
    outcome = out.get("outcome")
    if outcome == "phantom_marked":
        return (f"cms-regen {slug}: phantom registry — nothing consumes "
                f"run_now but the page updates via "
                f"{out.get('managed_by')} (updated {out.get('page_updated')}). "
                "Marked interval_min=0 + managed_by so the health check "
                "stops re-flagging it.")
    if outcome == "regenerated":
        return (f"cms-regen {slug}: regenerated — last_run "
                f"{out.get('last_run_after')}, last_status="
                f"{(out.get('health') or {}).get('last_status')}.")
    if outcome == "regen_error":
        return (f"cms-regen {slug}: regen ran but last_status="
                f"{(out.get('health') or {}).get('last_status')} — "
                f"{out.get('detail') or 'see registry doc'}.")
    if outcome == "stalled":
        return (f"cms-regen {slug}: STALLED — regen queued but last_run "
                f"never advanced and the page is stale "
                f"({out.get('page_updated')}). {out.get('detail') or ''} "
                "Needs a human: check the generator timer/logs on the "
                "owning host.".strip())
    if outcome == "no_registry":
        return (f"cms-regen {slug}: no ada-cms-automation doc — whatever "
                "flagged this card is gone; nothing to fix.")
    return (f"cms-regen {slug}: {outcome} — {out.get('detail') or ''}".strip())


# -- the contract --------------------------------------------------------------

def run(slug: str, *,
        api_base: str = CMS_API_URL,
        api_key: str | None = None,
        mddb_base: str = MDDB_BASE_URL,
        board_url: str = BOARD_API_URL,
        card_id: str | None = None,
        comms_from: str = "devin",
        managed_by: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        fresh_within_s: float = DEFAULT_FRESH_WITHIN_S,
        ledger_collection: str = HANDOFF_COLLECTION,
        ledger: bool = True,
        dry_run: bool = False,
        post_json: PostJson = _post_json,
        get_json=_get_json,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Regenerate one CMS automation and classify the outcome.

    Outcomes: regenerated | regen_error | phantom_marked | phantom_dryrun
    | stalled | generator_error | no_registry | regen_http_error.
    Returns a result dict; never raises for expected failure modes.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    slug = str(slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid page slug {slug!r}")
    if api_key is None:
        api_key = os.environ.get("ADA_API_KEY", "")
    if dry_run:
        # classify only — no registry write, comms, or ledger.
        card_id, ledger = None, False
    headers = {"x-api-key": api_key} if api_key else {}
    out: dict[str, Any] = {"ok": False, "slug": slug, "outcome": "error"}

    reg_status, doc, cfg = registry_get(mddb_base, slug, post_json)
    out["registry_doc"] = bool(doc)
    if reg_status == 0:
        out["outcome"] = "mddb_unreachable"
        out["detail"] = f"no response from {mddb_base}"
        return _finish(out, board_url, card_id, comms_from, mddb_base,
                       ledger_collection, ledger, post_json, get_json,
                       api_base, headers, now_fn)
    if doc is None:
        out["outcome"] = "no_registry"
        return _finish(out, board_url, card_id, comms_from, mddb_base,
                       ledger_collection, ledger, post_json, get_json,
                       api_base, headers, now_fn)
    out["last_run_before"] = cfg.get("last_run")

    page_before = page_get(mddb_base, slug, post_json)
    page_updated_before = _meta_first(page_before, "updated")

    # Command generators block the POST until done — the client timeout
    # must cover the server's own generator timeout.
    status, regen = post_regenerate(
        api_base, slug, headers, post_json, timeout=timeout_s + 30)
    out["regen_response"] = regen
    gen_result = (regen if status < 400 and isinstance(regen, dict)
                  and regen.get("generator") else None)
    if status == 422:
        # Declared generator the endpoint can't run — a doc-side phantom;
        # skip the poll and evaluate page freshness directly.
        out["detail"] = str(regen.get("detail") or "generator refused")
        return _evaluate_phantom(
            out, slug, cfg, page_updated_before, managed_by,
            fresh_within_s, dry_run, mddb_base, board_url, card_id,
            comms_from, ledger_collection, ledger, api_base, headers,
            post_json, get_json, now_fn)
    if status >= 400 or status == 0:
        out["outcome"] = "regen_http_error"
        out["detail"] = str(regen.get("detail") or f"HTTP {status}")
        return _finish(out, board_url, card_id, comms_from, mddb_base,
                       ledger_collection, ledger, post_json, get_json,
                       api_base, headers, now_fn)

    prev_last_run = cfg.get("last_run")
    # run_now counts as consumed only after we've SEEN it set — phantom
    # docs never carry it, so 'absent' is not the same as 'cleared'.
    saw_run_now = bool(cfg.get("run_now"))
    deadline = monotonic() + (
        GENERATOR_WRITEBACK_S if gen_result else timeout_s)
    advanced = False
    last_cfg = cfg
    while True:
        if monotonic() >= deadline:
            break
        sleep(poll_s)
        _, d, last_cfg = registry_get(mddb_base, slug, post_json)
        if not d:
            continue
        new_last_run = last_cfg.get("last_run")
        if new_last_run and new_last_run != prev_last_run:
            advanced = True
            break
        if last_cfg.get("run_now"):
            saw_run_now = True
        elif saw_run_now:
            # Cleared while we watched = a worker took the queue.
            advanced = True
            break
    out["last_run_after"] = last_cfg.get("last_run")
    out["last_status"] = last_cfg.get("last_status")

    if gen_result is not None and gen_result.get("status") != "done":
        out["outcome"] = ("generator_timeout"
                          if gen_result.get("status") == "timeout"
                          else "generator_error")
        out["detail"] = (
            f"generator '{gen_result.get('generator')}' "
            f"{gen_result.get('status')}"
            + (f" (exit {gen_result['exit_code']})"
               if gen_result.get("exit_code") is not None else "")
            + f": {str(gen_result.get('output') or '')[-300:]}")
    elif gen_result is not None:
        out["outcome"] = "regenerated" if advanced else "generator_done"
        if not advanced:
            out["detail"] = ("command generator finished but the registry "
                             "doc shows no write-back yet")
    elif advanced:
        out["outcome"] = ("regen_error"
                          if last_cfg.get("last_status") == "error"
                          else "regenerated")
        if out["outcome"] == "regen_error":
            out["detail"] = str(last_cfg.get("last_error") or "")
    else:
        return _evaluate_phantom(
            out, slug, last_cfg, page_updated_before, managed_by,
            fresh_within_s, dry_run, mddb_base, board_url, card_id,
            comms_from, ledger_collection, ledger, api_base, headers,
            post_json, get_json, now_fn)

    out["ok"] = out["outcome"] in ("regenerated", "generator_done")
    return _finish(out, board_url, card_id, comms_from, mddb_base,
                   ledger_collection, ledger, post_json, get_json,
                   api_base, headers, now_fn)


def _evaluate_phantom(out, slug, last_cfg, page_updated_before,
                      managed_by, fresh_within_s, dry_run, mddb_base,
                      board_url, card_id, comms_from, ledger_collection,
                      ledger, api_base, headers, post_json, get_json,
                      now_fn):
    """last_run never advanced — is the page bulk-timer-managed (phantom)
    or genuinely stalled? Fresh page = something else is writing it."""
    page_after = page_get(mddb_base, slug, post_json)
    page_updated = _meta_first(page_after, "updated")
    out["page_updated"] = page_updated
    window = _fresh_window(last_cfg, fresh_within_s)
    fresh = (_is_fresh(page_updated, window, now_fn())
             or (page_updated and page_updated != page_updated_before))
    if fresh:
        owner = (managed_by or _meta_first(page_after, "generated_by")
                 or _meta_first(page_after, "written_by")
                 or "external-timer")
        changes = _mark_managed(last_cfg, owner)
        out["managed_by"] = owner
        out["changes"] = changes
        if dry_run:
            out["outcome"] = "phantom_dryrun"
        elif changes and registry_save(mddb_base, slug, last_cfg,
                                       post_json, now_fn()):
            out["outcome"] = "phantom_marked"
            out["ok"] = True
        elif not changes:
            out["outcome"] = "phantom_marked"  # already marked
            out["ok"] = True
        else:
            out["outcome"] = "mark_failed"
            out["detail"] = "registry write-back failed"
    else:
        gen = last_cfg.get("generator") or {}
        out["outcome"] = "stalled"
        if isinstance(gen, dict) and gen.get("name"):
            out["detail"] = (f"registry declares generator "
                             f"{gen.get('kind') or '?'}:{gen.get('name')}")
    return _finish(out, board_url, card_id, comms_from, mddb_base,
                   ledger_collection, ledger, post_json, get_json,
                   api_base, headers, now_fn)


def _finish(out, board_url, card_id, comms_from, mddb_base,
            ledger_collection, ledger, post_json, get_json,
            api_base, headers, now_fn) -> dict[str, Any]:
    """Health re-check + comms + ledger. Side-effect failures are recorded
    on the result, never raised over the outcome itself."""
    health: dict[str, Any] = {
        "last_run": out.get("last_run_after"),
        "last_status": out.get("last_status"),
        "page_updated": out.get("page_updated"),
    }
    if out["outcome"] == "phantom_marked":
        health["state"] = "managed"
    elif out["outcome"] == "regenerated":
        health["state"] = "ok"
    elif out["outcome"] in ("stalled",):
        health["state"] = "stale"
    else:
        health["state"] = "error"
    if out["outcome"] not in ("no_registry", "regen_http_error"):
        verify = verify_page(api_base, out["slug"], headers, get_json)
        health["verify_ok"] = verify.get("ok") if verify else None
    out["health"] = health

    line = _comms_line(out)
    out["comms_line"] = line
    if card_id:
        out["comms_posted"] = board_comment(
            board_url, card_id, line, post_json, actor=comms_from)
    if ledger:
        key = write_ledger(mddb_base, ledger_collection, out["slug"],
                           out, post_json, now_fn(), card_id=card_id)
        if key:
            out["job_doc"] = key
        else:
            out["ledger_skipped"] = True
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Regenerate a CMS automation, verify write-back, "
                    "mark phantom registries (card cms-regen-autofix).")
    ap.add_argument("slug", help="page slug — for cms-auto-<slug> cards, "
                                 "the card id minus the prefix")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="max seconds to wait for last_run (default 300)")
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL_S,
                    help="registry poll interval seconds (default 10)")
    ap.add_argument("--managed-by",
                    help="owner tag for the phantom mark (default: the "
                         "page's generated_by/written_by meta)")
    ap.add_argument("--card", help="kanban card id — outcome posts to "
                                   "the card's comms")
    ap.add_argument("--from", dest="actor", default="devin",
                    help="comms actor name — board whitelist is ada/chaba/devin/tony (default devin)")
    ap.add_argument("--api", default=CMS_API_URL)
    ap.add_argument("--api-key", default=None,
                    help="default: $ADA_API_KEY")
    ap.add_argument("--mddb", default=MDDB_BASE_URL)
    ap.add_argument("--board", default=BOARD_API_URL)
    ap.add_argument("--no-ledger", action="store_true",
                    help="skip the job-ledger doc")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify only — no registry write, comms, ledger")
    ap.add_argument("--json", action="store_true",
                    help="print the full result dict")
    a = ap.parse_args()

    out = run(a.slug, api_base=a.api, api_key=a.api_key,
              mddb_base=a.mddb, board_url=a.board, card_id=a.card,
              comms_from=a.actor, managed_by=a.managed_by,
              timeout_s=a.timeout, poll_s=a.poll,
              ledger=not a.no_ledger, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(out.get("comms_line") or out["outcome"])
        if out.get("job_doc"):
            print(f"ledger: {out['job_doc']}")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
