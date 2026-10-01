"""traffic_camera — Thailand traffic cameras (Longdo/ITIC feed).

The Longdo feed (camera.longdo.com/feed/?command=json) lists ~190 live
traffic cams with lat/lon, Thai title, a still-JPEG endpoint (`imgurl`) and
an HLS url. Fetching a frame is one plain GET — no serial bottleneck like
the VMS shim — so this stays lazy: search + snap on demand, no polling.

Flow: match(query|lat,lon|heading) -> snap -> attach frame to the tool
result so Ada can describe it; also publish the frame as a same-origin
relay asset so cast_to_screen can put it on a vcast display.
"""
import base64
import json
import math
import os
import time
import urllib.parse
import urllib.request

FEED = os.environ.get(
    "TRAFFIC_CAM_FEED", "https://camera.longdo.com/feed/?command=json")
CACHE_TTL = 300  # feed changes slowly; cache 5 min
_cache: tuple[float, list[dict]] | None = None

AREA_ALIASES = {
    # English/spoken aliases -> Thai title keywords (Longdo titles are Thai).
    # Feed coverage (2026-09): ชลบุรี 84, ฉะเชิงเทรา 49, กรุงเทพ 27,
    # นนทบุรี 21, ขอนแก่น 12.
    "bangna": "บางนา", "bang na": "บางนา", "บางนา": "บางนา",
    "burapha": "บูรพาวิถี", "บูรพา": "บูรพาวิถี",
    "chonburi": "ชลบุรี", "ชลบุรี": "ชลบุรี",
    "chachoengsao": "ฉะเชิงเทรา", "ฉะเชิงเทรา": "ฉะเชิงเทรา",
    "nonthaburi": "นนทบุรี", "นนทบุรี": "นนทบุรี",
    "khonkaen": "ขอนแก่น", "khon kaen": "ขอนแก่น", "ขอนแก่น": "ขอนแก่น",
    "expressway": "ทางพิเศษ", "ทางพิเศษ": "ทางพิเศษ",
    "motorway": "มอเตอร์เวย์", "มอเตอร์เวย์": "มอเตอร์เวย์",
    "rama": "พระราม", "พระราม": "พระราม",
    "sukhumvit": "สุขุมวิท", "สุขุมวิท": "สุขุมวิท",
    "ratchada": "รัชดา", "รัชดา": "รัชดา",
    "sriracha": "ศรีราชา", "ศรีราชา": "ศรีราชา",
    "pattaya": "พัทยา", "พัทยา": "พัทยา",
    "jomtien": "จอมเทียน", "จอมเทียน": "จอมเทียน",
    "na kluea": "นาเกลือ", "naklua": "นาเกลือ", "นาเกลือ": "นาเกลือ",
    "kamphaeng": "กำแพงเพชร", "กำแพงเพชร": "กำแพงเพชร",
    "don muang": "ดอนเมือง", "ดอนเมือง": "ดอนเมือง",
    "bangkok": "กรุงเทพ", "กรุงเทพ": "กรุงเทพ",
}

# Direction words that appear inside camera titles — boost when the query
# or heading implies one.
DIR_WORDS = {
    "north": "เหนือ", "ทิศเหนือ": "เหนือ",
    "south": "ใต้", "ทิศใต้": "ใต้",
    "east": "ตะวันออก", "ทิศตะวันออก": "ตะวันออก",
    "west": "ตะวันตก", "ทิศตะวันตก": "ตะวันตก",
    "inbound": "ขาเข้า", "ขาเข้า": "ขาเข้า",
    "outbound": "ขาออก", "ขาออก": "ขาออก",
}


def _fetch_feed() -> list[dict]:
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_TTL:
        return _cache[1]
    with urllib.request.urlopen(FEED, timeout=20) as r:
        cams = json.loads(r.read())
    for c in cams:
        c["_lat"] = _f(c.get("latitude"))
        c["_lon"] = _f(c.get("longitude"))
        # suspended cams carry placeholder URLs (camid=X.X.X.X:YYYY,
        # hls_url=...tempsus.m3u8) — the feed lists them but they can never
        # produce a frame
        urls = str(c.get("imgurl")) + str(c.get("hls_url"))
        c["_live"] = "X.X.X.X" not in urls and "tempsus" not in urls
    _cache = (time.time(), cams)
    return cams


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dist_km(a_lat, a_lon, b_lat, b_lon) -> float:
    dx = math.radians(b_lat - a_lat)
    dy = math.radians(b_lon - a_lon) * math.cos(math.radians(a_lat))
    return math.hypot(dx, dy) * 6371.0


def _bearing(a_lat, a_lon, b_lat, b_lon) -> float:
    y = math.sin(math.radians(b_lon - a_lon)) * math.cos(math.radians(b_lat))
    x = (math.cos(math.radians(a_lat)) * math.sin(math.radians(b_lat))
         - math.sin(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.cos(math.radians(b_lon - a_lon)))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def find_cams(query: str = "", lat: float | None = None,
              lon: float | None = None, heading: float | None = None,
              limit: int = 5) -> list[dict]:
    """Score cameras: keyword hit beats distance; heading narrows to cams
    roughly ahead of the user (±60°)."""
    cams = _fetch_feed()
    q = (query or "").strip().lower()
    kw = AREA_ALIASES.get(q, q)
    # heading -> direction word (compass octants map onto Thai title words)
    if heading is not None and not any(k in q for k in DIR_WORDS):
        octant = ["north", "east", "east", "south", "south", "west", "west",
                  "north"][int((heading + 22.5) // 45) % 8]
        dir_kw = DIR_WORDS.get(octant)
    else:
        dir_kw = next((v for k, v in DIR_WORDS.items() if k in q), None)
    # tokenize the query — "ชลบุรี มอเตอร์เวย์" must match titles containing
    # either token, not only the whole phrase
    tokens = []
    for tok in (query or "").split():
        t = tok.strip().lower()
        if t:
            tokens.append(AREA_ALIASES.get(t, t))
    if kw and kw not in tokens:
        tokens.append(kw)
    suspended_hits = 0
    for c in cams:
        title = str(c.get("title") or "")
        c["_score"] = 0.0
        if not c.get("_live"):
            if tokens and any(t and t in title for t in tokens):
                suspended_hits += 1
            continue
        hits = sum(1 for t in tokens if t and t in title)
        c["_score"] += 60.0 * hits + (40.0 if hits == len(tokens) and tokens else 0)
        if dir_kw and dir_kw in title:
            c["_score"] += 40.0
        if lat is not None and lon is not None and c["_lat"] and c["_lon"]:
            d = _dist_km(lat, lon, c["_lat"], c["_lon"])
            c["_dist"] = round(d, 1)
            # base +60 so a geo-only query (no tokens) still ranks the
            # nearest cams — pure negative scoring returns "no match"
            c["_score"] += 60.0 - min(d, 100)
            if heading is not None:
                b = _bearing(lat, lon, c["_lat"], c["_lon"])
                diff = abs((b - heading + 180) % 360 - 180)
                if diff > 60:                   # behind us — deprioritize
                    c["_score"] -= 40
                else:
                    c["_score"] += 30 - diff / 2
        elif tokens:
            continue
        else:
            c["_score"] = 0
    ranked = [c for c in cams if c["_score"] > 0] if (tokens or lat is not None) \
        else [c for c in cams if c.get("_live")]
    ranked.sort(key=lambda c: -c["_score"])
    if suspended_hits and not ranked:
        return [{"camid": None, "title": f"{suspended_hits} camera(s) matched "
                 f"'{query}' but are marked suspended in the feed",
                 "suspended": True}]
    out = []
    for c in ranked[:limit]:
        out.append({
            "camid": c.get("camid"),
            "title": c.get("title"),
            "lat": c["_lat"], "lon": c["_lon"],
            "dist_km": c.get("_dist"),
            "imgurl": c.get("imgurl"),
            "vdourl": c.get("vdourl"),
            "hls_url": c.get("hls_url"),
            "org": c.get("organization"),
        })
    return out


def _mjpeg_first_frame(url: str, timeout: float) -> bytes:
    """Pull the first complete JPEG out of an MJPEG stream."""
    req = urllib.request.Request(url, headers={"User-Agent": "ada/1.0"})
    buf = b""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        end_at = time.time() + timeout
        while time.time() < end_at and len(buf) < 4 * 1024 * 1024:
            # read(), not read1(), waits to fill the whole 64KB buffer —
            # a dribbling dead-cam stream (~800B/s) stalls for minutes
            # without ever hitting the socket timeout. read1 returns
            # after a single recv so the end_at budget is honoured.
            chunk = r.read1(65536)
            if not chunk:
                break
            buf += chunk
            i = buf.find(b"\xff\xd8")
            if i >= 0:
                j = buf.find(b"\xff\xd9", i + 2)
                if j > 0:
                    return buf[i:j + 2]
    raise ValueError("no frame in mjpeg stream")


def snap(cam: dict, timeout: float = 20) -> tuple[bytes, str]:
    """Fetch the current still frame. jpeg2.php serves a 43B 'not found'
    stub on many cams — fall back to the MJPEG stream's first frame."""
    for url in (cam.get("imgurl"), cam.get("vdourl")):
        if not url:
            continue
        try:
            if "mjpeg" in url:
                data = _mjpeg_first_frame(url, timeout)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "ada/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read()
            # dead cams return a 43B text stub or a fixed ~3KB "No signal"
            # jpeg — real frames are ~20KB+; 8KB is a safe cutoff
            if len(data) >= 8000 and data.startswith(b"\xff\xd8"):
                return data, "image/jpeg"
        except Exception:
            continue
    raise ValueError("camera returned no usable frame (may be offline)")


def publish_relay(jpeg: bytes, slug: str) -> str | None:
    """POST the frame as a screen-0 relay asset; returns cast URL."""
    base = os.environ.get(
        "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")
    pub = os.environ.get(
        "VCAST_PUBLIC_API",
        "https://tony-dell.taila0626a.ts.net/api/input-bridge")
    token = f"traffic:{slug}-{int(time.time())}"
    try:
        req = urllib.request.Request(
            base + "/frame",
            data=json.dumps({
                "screen": 0, "token": token,
                "data": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                "state": "asset"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return f"{pub}/frame?screen=0&token={urllib.parse.quote(token)}"
    except Exception:
        return None
