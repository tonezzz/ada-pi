#!/usr/bin/env python3
"""secret-inventory — rotation inventory: which keys live where.

Lists ~/.config/secrets on each fleet host WITHOUT values — name, mode,
size, mtime, and a sha256[:8] fingerprint (the same sha8 the secret-drop
receipt returns, so inventory lines can be matched to drop receipts and
drift between hosts is visible). See docs/kb/ada-secrets-hygiene.md §4.

    secret-inventory.py [--json] [host ...]

Hosts default to the secret-drop fleet (idc03 tony-dell tony-omen idc02).
The local host is read directly; other hosts are read over ssh — the
remote command emits only metadata lines. Flags: !mode for non-0600
files, !sha-collision when two hosts disagree on sha8 for the same name.

Env: ADA_SECRETS_DIR overrides the local dir; ADA_HOST_ALIAS marks an
extra name as local.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HOSTS = ("idc03", "tony-dell", "tony-omen", "idc02")


def _local_names() -> set[str]:
    names = {socket.gethostname().split(".")[0].lower(), "localhost"}
    alias = os.environ.get("ADA_HOST_ALIAS", "").strip().lower()
    if alias:
        names.add(alias)
    return names


def _secrets_dir() -> Path:
    return Path(os.environ.get(
        "ADA_SECRETS_DIR", os.path.expanduser("~/.config/secrets")))


def _scan_local() -> list[dict]:
    d = _secrets_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        try:
            st = p.stat()
            sha8 = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
            out.append({"name": p.name, "mode": oct(st.st_mode & 0o777),
                        "bytes": st.st_size, "sha8": sha8,
                        "mtime": int(st.st_mtime)})
        except OSError:
            continue
    return out


_REMOTE = (
    'd="$HOME/.config/secrets"; [ -d "$d" ] || exit 0; '
    'for f in "$d"/*; do [ -f "$f" ] || continue; '
    'm=$(stat -c %a "$f"); s=$(stat -c %s "$f"); '
    't=$(stat -c %Y "$f"); h=$(sha256sum "$f" | cut -c1-8); '
    'echo "$(basename "$f") $m $s $t $h"; done')


def _scan_remote(host: str) -> list[dict]:
    """Metadata-only listing over ssh — mode/size/mtime/sha8, no values."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             host, "sh", "-c", _REMOTE],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutError) as exc:
        return [{"error": f"ssh: {exc}"}]
    if proc.returncode != 0:
        return [{"error": (proc.stderr.strip() or
                           f"exit {proc.returncode}")[:200]}]
    out = []
    for line in proc.stdout.splitlines():
        parts = line.rsplit(" ", 4)
        if len(parts) != 5:
            continue
        name, mode, size, mtime, sha8 = parts
        out.append({"name": name, "mode": "0" + mode[-3:],
                    "bytes": int(size), "sha8": sha8,
                    "mtime": int(mtime)})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hosts", nargs="*", default=list(HOSTS))
    ap.add_argument("--json", action="store_true", help="JSON output")
    a = ap.parse_args(argv)
    local = _local_names()
    inv: dict[str, list[dict]] = {}
    for host in a.hosts:
        host = host.lower()
        inv[host] = (_scan_local() if host in local
                     else _scan_remote(host))
    if a.json:
        print(json.dumps(inv, indent=1))
        return 0
    by_name: dict[str, dict[str, str]] = {}
    for host, files in inv.items():
        for f in files:
            if "name" in f:
                by_name.setdefault(f["name"], {})[host] = f["sha8"]
    rc = 0
    for host in a.hosts:
        host = host.lower()
        files = inv[host]
        err = next((f["error"] for f in files if "error" in f), None)
        if err:
            print(f"{host}: UNREACHABLE — {err}")
            rc = 1
            continue
        print(f"{host}: {len(files)} secret(s)")
        for f in files:
            flag = "" if f["mode"] in ("0600", "0o600", "0400") \
                else "  !mode"
            others = [v for k, v in by_name[f["name"]].items()
                      if k != host]
            if others and any(v != f["sha8"] for v in others):
                flag += "  !sha-mismatch"
            if flag.strip():
                rc = 1
            print(f"  {f['name']:<40} mode={f['mode']} "
                  f"bytes={f['bytes']} sha8={f['sha8']}{flag}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
