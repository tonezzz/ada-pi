"""place_report — turn a Google Maps link (or a place name) into a public
CMS report page and hand back the link to send in chat.

The kk lane sees members paste maps.app.goo.gl / google.com/maps links in
LINE and ask "ร้านนี้ยังอยู่มั้ย", "สรุปที่นี่หน่อย", "จุดนี้ล่ะ" — one ask
expects a page back (KNB-0574). This tool does the whole deterministic
half in one call: expand the short URL, resolve name/coords, enrich with
Nominatim (no key), publish a public WordPress post on the boards site
via wp_client, and return public_url for the chat reply. The model still answers the actual
question in chat — the page is the durable artifact.

Env:
    ADA_PLACE_REPORT_NOMINATIM — base URL (default OSM public endpoint)
    ADA_PLACE_REPORT_UA — User-Agent for outbound calls
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from typing import Any

import httpx

from backend import wp_client as _wp

NOMINATIM = os.environ.get(
    "ADA_PLACE_REPORT_NOMINATIM",
    "https://nominatim.openstreetmap.org")
UA = os.environ.get(
    "ADA_PLACE_REPORT_UA",
    "chaba-ada-place-report/1.0 (LINE maps-link resolver)")

DECLARATION = {
    "name": "place_report",
    "description": (
        "Turn a Google Maps link or place name into a public WordPress "
        "report post (map image + links); returns public_url to send "
        "in chat."),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The Google Maps link the user sent "
                               "(maps.app.goo.gl short links are "
                               "expanded). Omit when only a place name "
                               "is known.",
            },
            "place": {
                "type": "string",
                "description": "Place name/address to look up when no "
                               "maps URL was given.",
            },
            "ask": {
                "type": "string",
                "description": "What the user wants to know, verbatim "
                               "(e.g. 'is this shop still open?') — "
                               "recorded on the page as the question.",
            },
            "lang": {
                "type": "string",
                "description": "Page language: 'th' or 'en' (default th "
                               "for kk lane chats).",
            },
        },
        "additionalProperties": False,
    },
}

_MAPS_URL_RE = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.\S+|"
    r"www\.google\.\S*/maps)\S*")


def _parse_maps_url(url: str) -> tuple[str, str]:
    """'name, coords' from a resolved maps URL — mirrors the runner's
    _place_from_maps_url but returns the pair."""
    name = ""
    m = re.search(r"/place/([^/@?]+)", url)
    if m:
        name = urllib.parse.unquote_plus(m.group(1))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if not name and q.get("q"):
        name = q["q"][0]
    m2 = (re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
          or re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url))
    coords = f"{m2.group(1)},{m2.group(2)}" if m2 else ""
    return name, coords


async def _expand(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url)
    return str(r.url)


async def _nominatim(client: httpx.AsyncClient, path: str,
                     params: dict[str, Any]) -> Any:
    r = await client.get(f"{NOMINATIM}{path}", params=params,
                         headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()


async def _resolve(runner: Any, url: str, place: str
                   ) -> dict[str, Any]:
    """name / coords / address / display_name / resolved_url."""
    out: dict[str, Any] = {"name": place.strip(), "coords": "",
                           "address": "", "display_name": "",
                           "resolved_url": url.strip()}
    async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": UA}) as client:
        if url:
            try:
                resolved = await _expand(client, url)
                out["resolved_url"] = resolved
                name, coords = _parse_maps_url(resolved)
                out["name"] = out["name"] or name
                out["coords"] = coords
            except Exception as exc:
                return {"error": f"maps link did not resolve "
                                 f"({type(exc).__name__}) — the URL may "
                                 "need expanding by hand"}
        if out["coords"]:
            lat, lon = out["coords"].split(",", 1)
            try:
                d = await _nominatim(client, "/reverse", {
                    "format": "jsonv2", "lat": lat, "lon": lon,
                    "zoom": 18, "addressdetails": 1})
                out["display_name"] = d.get("display_name") or ""
                out["address"] = d.get("display_name") or ""
                if not out["name"]:
                    addr = d.get("address") or {}
                    out["name"] = (d.get("name")
                                   or next(iter(addr.values()), ""))
            except Exception:
                pass  # coords-only page is still useful
        elif out["name"]:
            # Google share strings lead with plus-code / house-number
            # tokens ('3302 PQRJ 452 JJ&J Bistro ...') — strip leading
            # code tokens before asking Nominatim, then the business
            # name alone. Small local shops legitimately miss; a
            # coords-empty page still carries the working maps link.
            cleaned = _strip_code_tokens(out["name"])
            found = False
            for q in dict.fromkeys(x for x in (out["name"], cleaned)
                                   if x):
                try:
                    rows = await _nominatim(client, "/search", {
                        "format": "jsonv2", "q": q, "limit": 1,
                        "addressdetails": 1})
                    if rows:
                        d = rows[0]
                        out["coords"] = f"{d['lat']},{d['lon']}"
                        out["display_name"] = d.get("display_name") or ""
                        out["address"] = d.get("display_name") or ""
                        out["name"] = d.get("name") or cleaned or out["name"]
                        found = True
                        break
                except Exception:
                    break
            if not found:
                out["name"] = _short_name(out["name"])
    return out


def _strip_code_tokens(name: str) -> str:
    """Drop leading whole tokens that look like plus-codes or house
    numbers ('3302 PQRJ 452 JJ&J Bistro' -> 'JJ&J Bistro ...') — whole
    tokens only so 'JJ&J' itself survives."""
    toks = name.split()
    while toks and re.fullmatch(r"[0-9A-Z]{2,8}\+?[0-9A-Z]*", toks[0]):
        toks.pop(0)
    return " ".join(toks) or name


def _short_name(name: str) -> str:
    """'JJ&J Bistro Cafe and Bar' from a q= blob — leading code tokens
    off, address tail after the first comma or bare-number run off."""
    name = _strip_code_tokens(name).split(",", 1)[0]
    toks = name.split()
    for i, tok in enumerate(toks):
        if i and re.fullmatch(r"\d+(?:/\d+)?", tok):
            toks = toks[:i]
            break
    return " ".join(toks) or name


def _staticmap(coords: str) -> str:
    if not coords:
        return ""
    return ("https://staticmap.openstreetmap.de/staticmap.php"
            f"?center={coords}&zoom=15&size=640x360"
            f"&markers={coords},red-pushpin")




def _person_for(runner: Any) -> str:
    """Lane instance -> WP person tag (kk-dev -> kk, ha-tony -> tony)."""
    inst = str(getattr(runner, "_instance_id", "") or "").lower()
    if "nhong" in inst:
        return "nhong"
    if "kk" in inst:
        return "kk"
    return "tony"


_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_IMGLINK = re.compile(r"\[!\[[^\]]*\]\(([^)]+)\)\]\(([^)]+)\)")


def _md_html(md: str) -> str:
    """Minimal md -> html for WP: image-links, links, headings, paras."""
    out = []
    for ln in md.split("\n"):
        s_ln = ln.strip()
        if not s_ln:
            continue
        if s_ln.startswith("## "):
            out.append(f"<h2>{_md_inline(s_ln[3:])}</h2>")
        elif s_ln.startswith("# "):
            continue  # title already set on the post
        else:
            out.append(f"<p>{_md_inline(s_ln)}</p>")
    return "\n".join(out)


def _md_inline(t: str) -> str:
    t = (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    t = _MD_IMGLINK.sub(r'<a href="\2"><img src="\1" style="max-width:100%"/></a>', t)
    t = _MD_IMG.sub(r'<img src="\1" style="max-width:100%"/>', t)
    t = _MD_LINK.sub(r'<a href="\2">\1</a>', t)
    return t

def _slug_for(runner: Any, name: str, coords: str) -> str:
    base = name or coords or "place"
    seed = hashlib.sha1(f"{name}|{coords}".encode()).hexdigest()[:6]
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40]
    slug = f"place-{base}-{seed}"[:64]
    slugify = getattr(runner, "_cms_slug", None)
    if callable(slugify):
        try:
            slug = slugify(slug)
        except Exception:
            pass
    return slug


def _page(place: dict[str, Any], ask: str, lang: str) -> tuple[str, str]:
    name = place["name"] or place["display_name"].split(",")[0] or "Place"
    # ssot.wp-boards title standard: machine CPT items get kind prefix
    title = f"Place — {name}" if lang == "en" else f"Place — {name} (รายงานสถานที่)"
    lines = [f"# {name}", ""]
    if place["address"]:
        lines += [place["address"], ""]
    if place["coords"]:
        lines += [f"Coordinates: {place['coords']}", ""]
    sm = _staticmap(place["coords"])
    gmaps = place["resolved_url"] or (
        f"https://www.google.com/maps/search/?api=1&query={place['coords']}"
        if place["coords"] else "")
    if sm and gmaps:
        lines += [f"[![map]({sm})]({gmaps})", ""]
    links = []
    if gmaps:
        links.append(f"[Google Maps]({gmaps})")
    if place["coords"]:
        lat, lon = place["coords"].split(",", 1)
        links.append(
            f"[OpenStreetMap](https://www.openstreetmap.org/"
            f"?mlat={lat}&mlon={lon}#map=16/{lat}/{lon})")
    if links:
        lines += [" · ".join(links), ""]
    if ask.strip():
        label = "คำถาม" if lang == "th" else "Question"
        lines += [f"## {label}", "", ask.strip(), ""]
    return title, "\n".join(lines)


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    place = str(args.get("place") or "").strip()
    ask = str(args.get("ask") or "")
    lang = str(args.get("lang") or "th").strip().lower() or "th"
    if not url and not place:
        m = _MAPS_URL_RE.search(ask)
        if m:
            url = m.group(0)
        else:
            return {"ok": False,
                    "error": "need a maps url or a place name — pass the "
                             "link the user sent as url"}
    resolved = await _resolve(runner, url, place)
    if resolved.get("error"):
        return {"ok": False, "error": resolved["error"]}
    if not resolved["name"] and not resolved["coords"]:
        return {"ok": False,
                "error": "the link resolved but carried no place name "
                         "or coordinates — ask the user for the place "
                         "name instead of the bare link"}
    title, content = _page(resolved, ask, lang)
    slug = _slug_for(runner, resolved["name"], resolved["coords"])
    summary = (f"Place report: {resolved['name'] or resolved['coords']} — "
               f"{(resolved['address'] or '')[:80]}")
    person = _person_for(runner)
    env = _wp.env()
    if not _wp.configured(env):
        return {"ok": False,
                "error": "wp-boards.env missing on this host — say the "
                         "page could not be saved, don't claim it was sent"}
    try:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as client:
            tag_ids = [t for t in (
                await _wp.ensure_tag(client, env, person),
            ) if t]
            post = await _wp.create_post(
                client, env, title, _md_html(content),
                tag_ids=tag_ids, status="publish", post_type="place")
    except Exception as exc:
        return {"ok": False,
                "error": f"wp post failed ({type(exc).__name__}): "
                         f"{exc} — say the page could not be saved, "
                         "don't claim it was sent"}
    if not post.get("ok"):
        return {"ok": False,
                "error": f"wp post failed: {post.get('error')} — say the "
                         "page could not be saved, don't claim it was sent"}
    out = {
        "ok": True,
        "slug": slug,
        "name": resolved["name"],
        "address": resolved["address"],
        "coords": resolved["coords"],
        "resolved_url": resolved["resolved_url"],
        "wp_id": post.get("id"),
        "public_url": post.get("link"),
    }
    out["note"] = (
        "send public_url back in the chat — it is already live on "
        "boards.surf-thailand.com. If they need the raw location too, "
        "also send the Google Maps link itself.")
    return out
