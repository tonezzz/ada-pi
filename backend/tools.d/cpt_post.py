"""cpt_post — save a structured record to the right boards WordPress
custom post type instead of a free-form wp_post.

LINE "save this" asks classify to a record kind (card
line-cpt-writer-router): a receipt photo -> expense, "save this contact"
-> contact, "remind us ..." -> reminder, "asset: CR-V" -> asset. LINE
photos arrive as real image turns, so the model reads the receipt in the
turn and passes the extracted fields — this tool composes the standard
title, writes the CPT post + its registered meta, and uploads the
archived photo as featured_media.

The CPTs' post_tag taxonomy is NOT registered (chaba-boards plugin), so
attribution rides the saved_by meta instead of wp_post's person tag.
contact/asset upsert by slug (re-saving the same contact updates it);
expense/reminder always create — each receipt/reminder is a new event.

Env (wp-boards.env on the backend host): same as wp_post —
    WP_SITE_URL, WP_APP_USER, WP_APP_PASSWORD
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from backend import wp_client as wp

KINDS = ("expense", "contact", "reminder", "asset")

# Registered REST-writable meta per kind — mirrors the register_post_meta
# table in chaba-boards.php. saved_by landed in plugin v0.5.1; writes
# degrade on a meta 400 (saved_by first, then all meta) so <= v0.5.0
# deploys still record the save.
_KIND_META = {
    "expense":  {"amount", "currency", "vendor", "paid_by",
                 "receipt_date", "saved_by"},
    "contact":  {"phone", "line_id", "role", "saved_by"},
    "reminder": {"due_date", "assignee", "saved_by"},
    "asset":    {"model", "serial", "purchase_date", "warranty_until",
                 "vendor", "saved_by"},
}

# Entity kinds re-save over time — upsert by slug instead of stacking
# duplicates. expense/reminder are events: always a new record.
_UPSERT_KINDS = {"contact", "asset"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _title(kind: str, name: str, meta: dict[str, str]) -> str:
    """title_standard from ssot.wp-boards.yml — machine content carries a
    '<Kind> — <name>' prefix so mixed feeds stay self-describing."""
    if kind == "expense":
        vendor = meta.get("vendor") or name or "receipt"
        date = meta.get("receipt_date") or ""
        return f"Expense — {vendor} {date}".rstrip()
    label = {"contact": "Contact", "reminder": "Reminder",
             "asset": "Asset"}[kind]
    return f"{label} — {name}"


def _slug(kind: str, name: str) -> str:
    """Deterministic slug for upsert lookups. Thai-only names slugify to
    nothing (WP strips non-ascii), so fall back to a short hash — the
    same contact name must land on the same record."""
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not s:
        s = hashlib.md5(name.encode()).hexdigest()[:8]
    return f"{kind}-{s}"[:60].rstrip("-")


def _person(runner: Any, arg: str) -> str:
    """Save attribution: explicit person= -> the lane's own member
    (ADA_INSTANCE_ID — kk's lane saves as kk) -> the caller identity
    (person.kk / user-kk normalize to kk). Empty when nothing maps to a
    known member — the record is still valid without it."""
    if arg in wp.PEOPLE:
        return arg
    inst = getattr(runner, "instance", "") or ""
    if inst in wp.PEOPLE:
        return inst
    if inst.endswith("-dev") and inst[:-4] in wp.PEOPLE:
        return inst[:-4]
    ident = ""
    try:
        ident = str(runner._memory_identity() or "")
    except Exception:
        pass
    ident = ident.removeprefix("person.").removeprefix("user-").lower()
    return ident if ident in wp.PEOPLE else ""


def _collect_meta(kind: str, person: str,
                  args: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Args -> registered meta for the kind; drops keys the kind doesn't
    own and reports them so the reply can say what wasn't stored."""
    meta: dict[str, str] = {}
    dropped: list[str] = []
    for k, v in args.items():
        if k in ("kind", "person", "title", "name", "content", "status",
                 "media_url", "slug", "confirmed", "confirm_token"):
            continue
        v = str(v).strip()
        if not v:
            continue
        if k in _KIND_META[kind]:
            meta[k] = v
        else:
            dropped.append(k)
    if person and "saved_by" in _KIND_META[kind] and "saved_by" not in meta:
        meta["saved_by"] = person
    return meta, dropped


async def _find_by_slug(client: httpx.AsyncClient, env: dict[str, str],
                        kind: str, slug: str) -> int | None:
    r = await wp.api(client, env, "GET", kind,
                     params={"slug": slug, "status": "any", "per_page": 1})
    rows = r.json() if r.status_code == 200 else []
    if isinstance(rows, list) and rows:
        return rows[0].get("id")
    return None


async def _create(client: httpx.AsyncClient, env: dict[str, str],
                  kind: str, title: str, body: str, slug: str,
                  status: str, media_id: int | None,
                  meta: dict[str, str]) -> dict[str, Any]:
    """Create, degrading meta on a 400 rather than losing the record.

    saved_by needs chaba-boards >= 0.5.1 — on a rest_invalid_meta the
    first retry drops just saved_by (every other key is registered
    since 0.3.x); if meta STILL rejects, drop it entirely. A version-
    skewed plugin must never eat the whole save."""
    for attempt, m in enumerate((meta,
                                {k: v for k, v in meta.items()
                                 if k != "saved_by"},
                                {})):
        if attempt and not m and attempt == 1:
            continue  # meta minus saved_by is empty — jump to no-meta
        res = await wp.create_post(client, env, title, body,
                                   status=status,
                                   featured_media=media_id,
                                   post_type=kind, meta=m or None,
                                   slug=slug)
        if res.get("ok"):
            res["meta_written"] = sorted(m)
            if attempt:
                res["meta_note"] = (
                    "saved_by skipped — live plugin <0.5.1" if attempt == 1
                    else "meta skipped — live plugin doesn't register "
                         "these keys yet")
            return res
        if not m or not re.search(r"meta", res.get("error") or "", re.I):
            return res
    return res


async def run(runner: Any, **args: Any) -> dict:
    kind = str(args.get("kind") or "").strip().lower()
    name = str(args.get("title") or args.get("name") or "").strip()
    content = str(args.get("content") or "").strip()
    status = str(args.get("status") or "private").strip().lower()
    media_url = str(args.get("media_url") or "").strip()
    person = _person(runner, str(args.get("person") or "").strip().lower())

    if kind not in KINDS:
        return {"ok": False,
                "error": f"kind must be one of {list(KINDS)} — free-form "
                         "posts go to wp_post"}
    if not name and kind != "expense":
        return {"ok": False, "error": f"title required for kind={kind}"}
    if status not in ("private", "publish", "draft"):
        return {"ok": False,
                "error": "status must be private|publish|draft"}
    meta, dropped = _collect_meta(kind, person, args)
    if kind == "expense" and "currency" not in meta:
        meta["currency"] = "THB"

    env = wp.env()
    if not wp.configured(env):
        return {"ok": False,
                "error": "wp-boards.env missing WP_SITE_URL/"
                         "WP_APP_USER/WP_APP_PASSWORD on this host"}

    title = _title(kind, name, meta)
    slug = str(args.get("slug") or "").strip().lower() or _slug(kind, name or title)

    try:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as client:
            media_id, media_src, media_note = None, "", ""
            if media_url:
                local = wp.media_local_path(media_url)
                if not local:
                    media_note = " (photo skipped — not found in archive)"
                else:
                    media_id, media_src = await wp.upload_media(
                        client, env, local)
                    if not media_id:
                        media_note = " (photo upload failed)"

            body = (f'<p><img src="{media_src}" alt=""/></p>\n'
                    if media_src else "") + wp.paragraphs(content)

            existing = (await _find_by_slug(client, env, kind, slug)
                        if kind in _UPSERT_KINDS else None)
            if existing:
                payload: dict[str, Any] = {"title": title,
                                           "content": body,
                                           "status": status,
                                           "meta": meta}
                if media_id:
                    payload["featured_media"] = media_id
                meta_note, written = "", meta
                r = await wp.api(client, env, "POST",
                                 f"{kind}/{existing}", json=payload)
                # Same degrade-on-400 as _create: drop saved_by first
                # (registered only in plugin >=0.5.1), then all meta.
                for m in ({k: v for k, v in meta.items()
                           if k != "saved_by"}, None):
                    if r.status_code == 200 or not meta or \
                            "meta" not in (r.text or ""):
                        break
                    written = m or {}
                    if m:
                        payload["meta"] = m
                    else:
                        payload.pop("meta", None)
                    r = await wp.api(client, env, "POST",
                                     f"{kind}/{existing}", json=payload)
                    meta_note = (
                        "saved_by skipped — live plugin <0.5.1" if m
                        else "meta skipped — live plugin doesn't "
                             "register these keys yet")
                if r.status_code != 200:
                    return {"ok": False,
                            "error": f"{kind} update failed "
                                     f"{r.status_code}: {r.text[:160]}"}
                note = " ".join(x for x in (media_note.strip(), meta_note)
                                if x)
                if dropped:
                    note = (note + " " if note else "") + \
                           f"ignored fields: {dropped}"
                return {"ok": True, "id": existing,
                        "url": wp.https(r.json().get("link") or ""),
                        "kind": kind, "title": title, "status": status,
                        "updated": True, "meta_written": sorted(written),
                        "media_url": media_src or None,
                        "note": note or None}

            res = await _create(client, env, kind, title, body, slug,
                                status, media_id, meta)
            if not res.get("ok"):
                return res
            meta_note = res.pop("meta_note", None)
            note = " ".join(x for x in (media_note.strip(), meta_note)
                            if x) or None
            if dropped:
                note = (note or "") + f" ignored fields: {dropped}"
            return {"ok": True, "id": res.get("id"),
                    "url": res.get("link"), "kind": kind, "title": title,
                    "status": status, "created": True,
                    "meta_written": res.get("meta_written") or [],
                    "media_url": media_src or None,
                    "note": note or None}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


DECLARATION = {
    "name": "cpt_post",
    "description": (
        "Save a structured record on the boards WP site — receipt/"
        "expense photo -> expense, 'save this contact' -> contact, "
        "'remind us' -> reminder, 'asset: X' -> asset. Free-form 'post "
        "this' asks stay on wp_post. Returns the record URL."),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "Record type — expense | contact | "
                               "reminder | asset.",
            },
            "title": {
                "type": "string",
                "description": "Name/subject — contact's name, reminder "
                               "text, asset name. Optional for expense "
                               "(vendor+date compose the title).",
            },
            "content": {
                "type": "string",
                "description": "Optional extra detail for the record "
                               "body (Thai ok).",
            },
            "person": {
                "type": "string",
                "description": "Member the record belongs to "
                               "(tony|kk|nhong) — saved_by meta. "
                               "Defaults to the lane's own member.",
            },
            "media_url": {
                "type": "string",
                "description": "Optional /relay-img/arch/... URL of a "
                               "photo just sent — receipt shot for "
                               "expense, photo/manual for asset.",
            },
            "status": {
                "type": "string",
                "enum": ["private", "publish", "draft"],
                "description": "Record visibility (default 'private' — "
                               "member login required; 'publish' is "
                               "public on the site).",
            },
            # Per-kind meta — pass only what the kind uses; fields for
            # other kinds are ignored (result lists them).
            "amount": {
                "type": "string",
                "description": "expense: amount paid, e.g. '320' or "
                               "'320.50'. Read it off the receipt.",
            },
            "currency": {
                "type": "string",
                "description": "expense: ISO code (default THB).",
            },
            "vendor": {
                "type": "string",
                "description": "expense/asset: shop or supplier name "
                               "from the receipt/manual.",
            },
            "paid_by": {
                "type": "string",
                "description": "expense: who paid (tony|kk|nhong) — "
                               "defaults to nobody; set when known.",
            },
            "receipt_date": {
                "type": "string",
                "description": "expense: date on the receipt "
                               "(YYYY-MM-DD preferred).",
            },
            "phone": {
                "type": "string",
                "description": "contact: phone number.",
            },
            "line_id": {
                "type": "string",
                "description": "contact: LINE id.",
            },
            "role": {
                "type": "string",
                "description": "contact: what they do — 'plumber', "
                               "'Honda service', 'insurance'.",
            },
            "due_date": {
                "type": "string",
                "description": "reminder: when it's due "
                               "(YYYY-MM-DD preferred, time ok).",
            },
            "assignee": {
                "type": "string",
                "description": "reminder: who it's for "
                               "(tony|kk|nhong).",
            },
            "model": {
                "type": "string",
                "description": "asset: model name/number, e.g. "
                               "'CR-V 2.0 EL'.",
            },
            "serial": {
                "type": "string",
                "description": "asset: serial number.",
            },
            "purchase_date": {
                "type": "string",
                "description": "asset: purchase date (YYYY-MM-DD).",
            },
            "warranty_until": {
                "type": "string",
                "description": "asset: warranty expiry (YYYY-MM-DD).",
            },
        },
        "required": ["kind"],
    },
}
