#!/usr/bin/env python3
"""Recall-path latency bench — mddb (idc01) vs weaviate (tony-dell).

Run on idc01 (mddb local + weaviate over tailnet):

    ADA_MDDB=http://127.0.0.1:11023 python3 tests/bench_recall.py
    python3 tests/bench_recall.py --json          # machine-readable
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request

MDDB = os.environ.get("ADA_MDDB", "http://100.74.146.0:11023")
WEAVIATE = os.environ.get("WEAVIATE_URL", "http://100.68.142.13:8084")
COLL = os.environ.get("BENCH_COLLECTION", "ada-ha-bank-general")


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=20).read()


def bench(fn, n):
    fn()  # warm-up
    ts = []
    for _ in range(n):
        t0 = time.time()
        fn()
        ts.append((time.time() - t0) * 1000)
    ts.sort()
    return {"n": n, "p50": round(ts[n // 2]), "p95": round(ts[int(n * 0.95)]),
            "min": round(ts[0]), "max": round(ts[-1])}


def mddb_vector():
    _post(f"{MDDB}/v1/vector-search",
          {"collection": COLL, "query": "things we discussed about the house", "top_k": 5})


def mddb_kw():
    _post(f"{MDDB}/v1/search",
          {"collection": COLL, "query": "house", "top_k": 5})


def mddb_get():
    _post(f"{MDDB}/v1/get",
          {"collection": COLL, "key": "preference/language-convention", "lang": "en"})


def weaviate_near():
    vec = [random.random() for _ in range(768)]
    _post(f"{WEAVIATE}/v1/graphql", {"query":
        "{Get{SSOTDocument(nearVector:{vector:%s},limit:5){_additional{id distance}}}}"
        % json.dumps(vec)})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = {}
    for name, fn in [("mddb_vector_search", mddb_vector), ("mddb_keyword", mddb_kw),
                     ("mddb_get", mddb_get), ("weaviate_nearvector", weaviate_near)]:
        try:
            out[name] = bench(fn, a.n)
        except Exception as exc:
            out[name] = {"error": str(exc)[:120]}
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        for k, v in out.items():
            if "error" in v:
                print(f"{k:<22} ERROR {v['error']}")
            else:
                print(f"{k:<22} p50={v['p50']}ms p95={v['p95']}ms "
                      f"min={v['min']} max={v['max']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
