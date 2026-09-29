#!/usr/bin/env python3
"""tour-prep — pre-flight a GEV camera tour off the camwall zones.

Reads the live camwall manifests (puller-refreshed frames) + the camera
registry (lat/lon) and emits:
  - tour-<name>.json — machine tour card: ordered stops with place, cam
    title, lat/lon, cached frame URL (consumable by tools/scenarios)
  - tour-<name>.html — presentation wall: one card per stop w/ the current
    frame (cast to a vcast pane or publish to CMS)

Usage:
  tour-prep.py --name bkk --zone rama9 --zone traffic [--zone burapha]
  tour-prep.py --name bkk --cam itic---rama-4-rd--expressway-interchange@traffic ...
Only live cams become stops; dead ones are listed in the report as skipped.
"""
import argparse, html, json, re, sys, time
import urllib.request
from pathlib import Path

BASE = "https://tony-dell.taila0626a.ts.net/apps/camwall/"
CAMERAS_JSON = Path("/home/tony/CascadeProjects/chaba/frigate/cameras.json")
GROUP_ZONE = {"Traffic": "traffic", "ทางพิเศษบูรพาวิถี": "burapha",
              "ชลบุรี": "chonburi"}


def slug(s: str) -> str:
    # must match cam-wall-pull.py exactly: every non-alnum char -> '-'
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def get(url: str):
    return json.load(urllib.request.urlopen(url, timeout=15))


def registry_by_zone() -> dict:
    """zone -> slug(title) -> registry cam"""
    reg = json.loads(CAMERAS_JSON.read_text())["cameras"]
    out = {}
    for c in reg:
        z = GROUP_ZONE.get(c.get("group"))
        if z:
            out.setdefault(z, {})[slug(c.get("title", ""))] = c
    return out


def collect_stops(zones, explicit) -> list[dict]:
    reg = registry_by_zone()
    stops, skipped = [], []
    want = {s.split("@")[0]: s.split("@")[1] for s in explicit or []}
    for zone in zones:
        man = get(f"{BASE}data/{zone}/manifest-{zone}.json")
        for c in man.get("cams", []):
            entry = {"zone": zone, "key": c["key"], "label": c["label"],
                     "frame_url": f"{BASE}data/{zone}/{c['key']}.jpg",
                     "ts": c.get("ts"), "live": bool(c.get("ok"))}
            rc = reg.get(zone, {}).get(c["key"], {})
            if rc.get("lat"):
                entry.update(lat=rc["lat"], lon=rc["lon"])
            if explicit:
                if c["key"] in want and want[c["key"]] == zone:
                    (stops if c.get("ok") else skipped).append(entry)
            else:
                (stops if c.get("ok") else skipped).append(entry)
    return stops, skipped


def render_html(name: str, stops: list[dict], skipped: list[dict]) -> str:
    cards = []
    for i, st in enumerate(stops, 1):
        loc = (f'{st["lat"]:.4f},{st["lon"]:.4f}' if st.get("lat")
               else st["zone"])
        cards.append(f'''<div class="card"><div class="n">{i}</div>
<img src="{st["frame_url"]}" /><div class="meta"><b>{html.escape(st["label"])}</b><br>
<span>{loc} · {st["zone"]}</span></div></div>''')
    for st in skipped:
        cards.append(f'''<div class="card dim"><div class="meta"><b>{html.escape(st["label"])}</b><br>
<span>{st["zone"]} — offline (skipped)</span></div></div>''')
    return f'''<!doctype html><meta charset="utf-8"><title>Tour: {html.escape(name)}</title>
<style>
body{{margin:0;background:#0b0e14;color:#e6edf3;font-family:system-ui}}
h1{{font-size:1.2rem;margin:.6em 1rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;padding:0 12px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden;position:relative}}
.card img{{width:100%;display:block}}
.card.dim{{opacity:.45}}
.n{{position:absolute;top:6px;left:6px;background:#1f6feb;border-radius:50%;width:26px;height:26px;text-align:center;line-height:26px;font-weight:700}}
.meta{{padding:.5em .7em;font-size:.85rem}}.meta span{{color:#8b949e}}
</style>
<h1>Tour report — {html.escape(name)} · {time.strftime("%Y-%m-%d %H:%M")} · {len(stops)} live stops</h1>
<div class="grid">{"".join(cards)}</div>'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--zone", action="append", default=[])
    ap.add_argument("--cam", action="append", default=[],
                    help="slug@zone to select a specific camera")
    ap.add_argument("--out-dir", default="/tmp")
    ap.add_argument("--publish", action="store_true",
                    help="scp the report HTML to the live vcast webroot")
    a = ap.parse_args()
    zones = a.zone or sorted({s.split("@")[1] for s in a.cam})
    if not zones:
        ap.error("pass --zone or --cam slug@zone")
    stops, skipped = collect_stops(zones, a.cam)
    tour = {"name": a.name, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stops": stops, "skipped": skipped}
    jp = Path(a.out_dir) / f"tour-{a.name}.json"
    hp = Path(a.out_dir) / f"tour-{a.name}.html"
    jp.write_text(json.dumps(tour, ensure_ascii=False, indent=1))
    hp.write_text(render_html(a.name, stops, skipped))
    if a.publish:
        # report wall goes to the live vcast webroot — castable as a pane
        dst = ("/home/tony/CascadeProjects/chaba-tony-dell/stacks/web/"
               f"public/apps/camwall/{hp.name}")
        import subprocess
        subprocess.run(["scp", "-q", str(hp), f"tony-dell:{dst}"],
                       check=True)
        print(f"  published: {BASE}{hp.name}")
    have_xy = sum(1 for s in stops if s.get("lat"))
    print(f"{len(stops)} live stops ({have_xy} with coords), "
          f"{len(skipped)} skipped/offline\n  {jp}\n  {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
