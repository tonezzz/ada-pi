#!/usr/bin/env python3
"""Bridge Devin session artifacts into Ada's MDDB memory — the recall path
that makes dispatch outcomes searchable from both Ada (ada_memory_search)
and Devin (mddb MCP).

NOTE: continuation-summary sync is already covered — chaba
scripts/ada/sync-devin-summaries.py runs hourly (devin-summaries-sync.
timer) and writes both summary dirs into ada-ha-bank-devin-<instance>.
This script only PUBLISHES artifacts that sync doesn't see:
dispatch-outcome.md, run records, ad-hoc reports.

Writes go to the MDDB PRIMARY (idc01); local tony-dell instance is a
read-only follower — reads/search work locally, writes need the primary
reachable.

Usage:
  python3 scripts/devin-memory-bridge.py dispatch-outcome.md --tag e155f30
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_CONTENT_CHARS = 60000  # keep docs under MDDB/embedding limits


async def _publish(client, collection: str, path: Path, key: str,
                   kind: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "\n\n[truncated — see file ref]"
    day = datetime.fromtimestamp(
        path.stat().st_mtime, timezone.utc).date().isoformat()
    res = await client.add_document(
        collection=collection, key=key, lang="en", content_md=text,
        meta={"kind": [kind], "date": [day], "source": ["devin"],
              "ref": [f"file:{path}"]})
    return res is not None


async def _main(args) -> int:
    from backend.mddb_client import MddbClient
    client = MddbClient()
    today = datetime.now(timezone.utc).date().isoformat()
    ok = fail = 0

    for f in args.files or []:
        tag = args.tag or f.stem.replace("_", "-")[:32]
        key = f"devin/{args.kind}/{today}-{tag}"
        if await _publish(client, args.collection, f, key, args.kind):
            print(f"published {f} -> mddb://{args.collection}/{key}")
            ok += 1
        else:
            fail += 1
            print(f"FAILED {f}", file=sys.stderr)
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--collection", default=None,
                    help="MDDB collection (default: ada-ha-bank-devin-"
                         "$ADA_INSTANCE_ID — the 'devin' bank)")
    ap.add_argument("--tag", default=None, help="key suffix for publish")
    ap.add_argument("--kind", default="report",
                    help="doc kind for publish (report, session-summary)")
    args = ap.parse_args()

    if not args.collection:
        from backend.instance import ada_instance_id
        args.collection = f"ada-ha-bank-devin-{ada_instance_id()}"
    if not args.files:
        ap.error("give files to publish")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
