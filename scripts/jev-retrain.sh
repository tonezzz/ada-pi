#!/usr/bin/env bash
# Nightly/weekly student retrain loop — closes the bench→corpus→model
# cycle. Corpus accumulates live in ~/.local/share/ada/jev-corpus.jsonl
# on idc01 (written by the advisory probe); this script merges it with
# the mined corpus, trains on idc02's 4-core box (torch CPU, ~20min),
# deploys the checkpoint back to idc01, restarts jev-student, and
# benches the new model. Run from anywhere that can ssh both hosts.
#
#   scripts/jev-retrain.sh           # full loop
#   scripts/jev-retrain.sh --no-deploy   # train + bench only
set -euo pipefail

IDC01=idc01
IDC02=idc02
STAMP=$(date +%Y%m%d-%H%M%S)
WORK=/tmp/jev-retrain-$STAMP
mkdir -p "$WORK"

echo "[1/5] harvest corpus from $IDC01"
ssh "$IDC01" 'cat ~/.local/share/ada/jev-corpus.jsonl' > "$WORK/jev-corpus.jsonl" 2>/dev/null || true
ssh "$IDC01" 'cat ~/.local/share/ada/jev-corpus-mined.jsonl' > "$WORK/jev-corpus-mined.jsonl" 2>/dev/null || true
ssh "$IDC01" 'cat ~/.local/share/ada/jev-corpus-reviewed.jsonl' > "$WORK/jev-corpus-reviewed.jsonl" 2>/dev/null || true
[ -s "$WORK/jev-corpus-mined.jsonl" ] || echo "" > "$WORK/jev-corpus-mined.jsonl"
[ -s "$WORK/jev-corpus-reviewed.jsonl" ] || echo "" > "$WORK/jev-corpus-reviewed.jsonl"
JEVDIR="$(cd "$(dirname "$0")" && pwd)/jev"
# merge w/ label policy (reviewed->eval, diverged/ambiguous dropped,
# negation relabels) + targeted failure-class augmentation
python3 "$JEVDIR/merge-corpus.py" "$WORK"
python3 "$JEVDIR/augment-corpus.py" "$WORK/augment.jsonl"
python3 - "$WORK" <<'PY'
import json, sys
d = sys.argv[1]
rows = [json.loads(l) for l in open(d + "/corpus-merged.jsonl")]
rows += [json.loads(l) for l in open(d + "/augment.jsonl")]
out = [{"text": r["text"], "label": bool(r["label"])} for r in rows]
json.dump(out, open(d + "/corpus.json", "w"), ensure_ascii=False)
print(f"train corpus: {len(out)} rows ({sum(1 for r in out if r['label'])} pos)")
PY

echo "[2/5] sync to $IDC02 + train"
scp -q "$WORK/corpus.json" "$IDC02:/tmp/jev-all.json"
scp -q "$(dirname "$0")/train-jev-student.py" "$IDC02:/tmp/train-student.py"
ssh "$IDC02" 'JEV_CORPUS=/tmp/jev-all.json ~/CascadeProjects/open-jev/.venv/bin/python /tmp/train-student.py 2>&1 | tail -6'

echo "[3/5] pull checkpoint back"
ssh "$IDC02" 'cd ~/jev-student && tar czf /tmp/ckpt.tgz ckpt'
scp -q "$IDC02:/tmp/ckpt.tgz" "$WORK/ckpt.tgz"

if [ "${1:-}" = "--no-deploy" ]; then
    echo "[skip] --no-deploy"
    exit 0
fi

echo "[4/5] deploy to $IDC01"
scp -q "$WORK/ckpt.tgz" "$IDC01:/tmp/ckpt-$STAMP.tgz"
ssh "$IDC01" "cd ~/jev-student && cp -r ckpt ckpt-bak-$STAMP 2>/dev/null; rm -rf ckpt && tar xzf /tmp/ckpt-$STAMP.tgz && systemctl --user restart jev-student && sleep 12 && curl -s http://100.74.146.0:8778/health"

echo "[5/5] bench the new model"
ssh "$IDC01" 'cd ~/CascadeProjects/ada-pi && timeout 300 .venv/bin/python tests/bench/jev-bench.py http://100.74.146.0:8778 --mddb http://100.74.146.0:11023/v1 --report-cms 2>&1 | tail -18'
echo "done — $WORK"
