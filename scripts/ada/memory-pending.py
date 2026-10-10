#!/usr/bin/env python3
"""memory-pending — approver surface for staged memory writes.

Card ada-memory-staged-writes: writes from non-{full:true} identities
park in the vault pending dir (backend/memory_pending.py) instead of
landing in a bank. This is the respond half of the review lane — the
notify half is the memory-staged line in the ada events digest (plus an
optional board comment when ADA_MEMORY_REVIEW_CARD is set).

Usage:

    python3 scripts/ada/memory-pending.py list
    python3 scripts/ada/memory-pending.py show  <id>
    python3 scripts/ada/memory-pending.py approve <id> [--by tony]
    python3 scripts/ada/memory-pending.py reject  <id> [--by tony] [--reason ...]

approve applies the pinned staged text verbatim through the original
write path — bank write -> memory_ops.remember, guest -> chaba store,
vocab -> the persona-bank vocab log — then marks the entry approved.
Entries are single-use: a second approve is refused.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _REPO_ROOT)

from backend import memory_pending  # noqa: E402


def _who(explicit: str | None) -> str:
    return explicit or os.environ.get("USER") or getpass.getuser() or "?"


def _fmt(entry: dict) -> str:
    ident = entry.get("identity") or "anonymous"
    route = entry.get("route") or "?"
    target = entry.get("bank") or (entry.get("guest") or {}).get("name") or "-"
    return (f"{entry['id']}  {route}/{target}  by={ident}  "
            f"at={entry.get('staged_at')}  sess={entry.get('source_session')}"
            f"\n    {memory_pending.preview(entry, 120)}")


def cmd_list(args: argparse.Namespace) -> int:
    entries = memory_pending.list_pending(args.dir)
    if args.json:
        print(json.dumps(entries, indent=1, ensure_ascii=False, default=str))
        return 0
    if not entries:
        print("no staged memory writes pending")
        return 0
    print(f"{len(entries)} staged write(s) pending:")
    for e in entries:
        print(_fmt(e))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    entry = memory_pending.load(args.id, args.dir)
    if entry is None:
        print(f"no pending entry {args.id!r}", file=sys.stderr)
        return 1
    print(json.dumps(entry, indent=1, ensure_ascii=False, default=str))
    return 0


def _runtime(entry: dict):
    """Build the stores the apply route needs — only for its route."""
    route = entry.get("route")
    mddb = registry = chaba = None
    if route in ("bank", "vocab"):
        from backend.mddb_client import MddbClient
        from backend.memory_banks import MemoryBankRegistry
        mddb = MddbClient()
        # An approved write lands in the entry's recorded instance's banks —
        # the registry resolves collections for THIS instance's ADA_INSTANCE_ID.
        registry = MemoryBankRegistry()
    if route in ("guest", "guest_private"):
        from backend.chaba_memory import ChabaMemory
        chaba = ChabaMemory()
    return mddb, registry, chaba


def cmd_approve(args: argparse.Namespace) -> int:
    entry = memory_pending.load(args.id, args.dir)
    if entry is None:
        print(f"no pending entry {args.id!r}", file=sys.stderr)
        return 1
    mddb, registry, chaba = _runtime(entry)
    res = asyncio.run(memory_pending.approve(
        args.id, mddb=mddb, registry=registry,
        instance=entry.get("instance"), chaba=chaba,
        by=_who(args.by), pending_dir=args.dir))
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1


def cmd_reject(args: argparse.Namespace) -> int:
    res = memory_pending.reject(
        args.id, by=_who(args.by), reason=args.reason, pending_dir=args.dir)
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=None,
                    help="pending dir override (default: "
                         "~/.local/share/ada/memory-pending)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sp = sub.add_parser("show")
    sp.add_argument("id")
    sp = sub.add_parser("approve")
    sp.add_argument("id")
    sp.add_argument("--by", default=None)
    sp = sub.add_parser("reject")
    sp.add_argument("id")
    sp.add_argument("--by", default=None)
    sp.add_argument("--reason", default=None)
    for p in (sub.choices["list"],):
        p.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return {"list": cmd_list, "show": cmd_show,
            "approve": cmd_approve, "reject": cmd_reject}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
