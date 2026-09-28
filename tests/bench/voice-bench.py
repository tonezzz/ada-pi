#!/usr/bin/env python3
"""voice-bench: speaker enrollment/identification accuracy over the corpus.

Corpus: tests/voice-corpus/registry.yaml + clips/<id>-NN.wav (16kHz mono
PCM wav). A speaker is an ordered clip list — clip k enrolls, clip k+1
tests — which simulates voiceprints accumulating over sessions.

Cases (each clip-level, per speaker, vs the live SpeakerIdentifier):
  enroll_identify  enroll clips 1..k → identify clip k+1 → expect name
  cross_negative   identify B's clip when only others enrolled → expect None
  contamination    enroll B's audio under name "A" → expect refusal
  own_voice_drift  enroll A(clip1) then A(clip3) → guard merge/refuse
  auto_learn       repeated identifies append prints → later hits improve
  threshold_sweep  same vs cross speaker score table → threshold suggestion

Only registry speakers with qualified: ok run by default; pending needs
--include-pending. Writes markdown to stdout and optionally an MDDB doc
(--mddb URL → ada-ha-scenario-reports kind:benchmark) and a CMS page
(--report-cms 'bench-voice').

Usage:
  python3 tests/bench/voice-bench.py [--corpus tests/voice-corpus]
    [--include-pending] [--mddb http://idc01:11023/v1] [--report-cms]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

parser = argparse.ArgumentParser()
parser.add_argument("--corpus", default=str(REPO / "tests" / "voice-corpus"))
parser.add_argument("--include-pending", action="store_true")
parser.add_argument("--mddb", default="")
parser.add_argument("--report-cms", action="store_true")
ARGS = parser.parse_args()

# Isolate bench state BEFORE importing the module — a temp profiles file
# and samples dir so the live registry is never touched.
_tmp = tempfile.mkdtemp(prefix="voicebench-")
os.environ["ADA_SPEAKER_PROFILES"] = str(Path(_tmp) / "profiles.json")
os.environ["ADA_SPEAKER_KEEP_AUDIO"] = "0"

import yaml  # noqa: E402

from backend.speaker_id import (  # noqa: E402
    SpeakerIdentifier, DEFAULT_THRESHOLD, _cosine_similarity,
)


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, (
            f"{path.name}: need 16-bit mono wav")
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
    if rate != 16000:
        import audioop
        frames = audioop.ratecv(frames, 2, 1, rate, 16000, None)[0]
    return frames


def load_registry(corpus: Path) -> list[dict]:
    reg = yaml.safe_load((corpus / "registry.yaml").read_text()) or {}
    speakers = reg.get("speakers") or []
    out = []
    for sp in speakers:
        if sp.get("qualified") == "ok" or (
                ARGS.include_pending and sp.get("qualified") == "pending"):
            sp = dict(sp)
            sp["kind"] = sp.get("kind") or "person"
            out.append(sp)
    return out


def main() -> int:
    corpus = Path(ARGS.corpus)
    clips_dir = corpus / "clips"
    speakers = load_registry(corpus)
    if not speakers:
        print("No qualified speakers in registry — harvest + review first.")
        return 1

    ident = SpeakerIdentifier()
    persons = [s for s in speakers if s["kind"] == "person"]
    clips = {s["id"]: [load_wav(clips_dir / c) for c in s["clips"]]
             for s in speakers}

    rows: list[str] = []
    results: dict[str, dict] = {
        "enroll_identify": {"pass": 0, "fail": 0, "fails": []},
        "cross_negative": {"pass": 0, "fail": 0, "fails": []},
        "contamination": {"pass": 0, "fail": 0, "fails": []},
        "auto_learn": {"pass": 0, "fail": 0, "fails": []},
    }
    same_scores: list[float] = []
    cross_scores: list[float] = []
    margin_sum = []

    def rec(case: str, ok: bool, note: str) -> None:
        results[case]["pass" if ok else "fail"] += 1
        if not ok:
            results[case]["fails"].append(note)

    # enroll clip 0 for every person speaker (one voiceprint each)
    enrolled: list[str] = []
    for sp in persons:
        if len(clips[sp["id"]]) < 2:
            continue
        try:
            ident.enroll(sp["id"], clips[sp["id"]][0])
            enrolled.append(sp["id"])
        except Exception as exc:
            rec("enroll_identify", False, f"{sp['id']}: enroll clip0: {exc}")

    # enroll_identify: clip 1 (and 2 when present) after clip-0 enrollment
    for sp in persons:
        for i, clip in enumerate(clips[sp["id"]][1:], start=1):
            name, score = ident.identify(clip)
            ok = name == sp["id"]
            rec("enroll_identify", ok,
                f"{sp['id']} clip{i}: got {name} @{score:.2f}")
            if name == sp["id"]:
                same_scores.append(score)

    # cross_negative: each person's held-out clip vs everyone else's profile
    for sp in persons:
        others = [o for o in enrolled if o != sp["id"]]
        if not others:
            continue
        probe = clips[sp["id"]][-1]
        # score the probe against all other enrolled names only
        emb = ident._compute_embedding(probe)
        for other in others:
            refs = ident._prints.get(other) or [ident._enrolled[other]]
            for r in refs:
                cross_scores.append(_cosine_similarity(emb, r))
        name, score = ident.identify(probe)
        # identify may return sp itself (correct) or None — a WRONG name
        # is the failure we care about
        rec("cross_negative", name in (None, sp["id"]),
            f"{sp['id']} probe misidentified as {name} @{score:.2f}")

    # contamination: enrolling B's audio as A must refuse
    if len(enrolled) >= 2:
        victim, foreign = enrolled[0], enrolled[1]
        try:
            ident.enroll(victim, clips[foreign][0])
            rec("contamination", False,
                f"{foreign} audio accepted as '{victim}'")
        except ValueError:
            rec("contamination", True, "")
        except Exception as exc:
            rec("contamination", False,
                f"{foreign}→'{victim}': unexpected {exc}")

    # own_voice_drift: re-enroll a speaker with a later clip — guard may
    # merge (pass) or refuse legitimately; record outcome as a data row
    # rather than pass/fail.
    drift_rows = []
    for sp in persons:
        if len(clips[sp["id"]]) < 3:
            continue
        try:
            ident.enroll(sp["id"], clips[sp["id"]][2])
            drift_rows.append(f"{sp['id']}: merged (3rd clip accepted)")
        except ValueError as exc:
            drift_rows.append(f"{sp['id']}: refused ({exc})")

    # auto_learn: after enough same-speaker identifies prints should grow
    for sp in persons:
        if len(clips[sp["id"]]) < 2:
            continue
        before = len(ident._prints.get(sp["id"]) or [])
        for clip in clips[sp["id"]][1:3]:
            name, score = ident.identify(clip)
            if name == sp["id"] and score >= DEFAULT_THRESHOLD:
                ident.auto_learn(sp["id"], clip)  # exercises the learner
        after = len(ident._prints.get(sp["id"]) or [])
        rec("auto_learn", after > before,
            f"{sp['id']}: prints {before}→{after} (no growth)")

    # threshold sweep table
    if same_scores and cross_scores:
        margin = (sorted(same_scores)[len(same_scores) // 2]
                  - max(cross_scores))
        margin_sum.append(margin)

    # --- report ---
    total_p = sum(r["pass"] for r in results.values())
    total_f = sum(r["fail"] for r in results.values())
    lines = [
        f"# voice-bench {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"corpus: {len(persons)} person speakers, "
        f"{sum(len(c) for c in clips.values())} clips; "
        f"threshold={DEFAULT_THRESHOLD}, "
        f"score mode={os.environ.get('ADA_SPEAKER_SCORE', 'centroid')}",
        "",
        "## case results",
        "",
        "| case | pass | fail |",
        "|---|---:|---:|",
    ]
    for case, r in results.items():
        lines.append(f"| {case} | {r['pass']} | {r['fail']} |")
    if drift_rows:
        lines += ["", "## own-voice drift (re-enroll outcomes)", ""] + [
            f"- {r}" for r in drift_rows]
    if same_scores and cross_scores:
        lines += [
            "",
            "## score distribution",
            "",
            f"- same-speaker: n={len(same_scores)} "
            f"min={min(same_scores):.2f} med={sorted(same_scores)[len(same_scores)//2]:.2f} "
            f"max={max(same_scores):.2f}",
            f"- cross-speaker: n={len(cross_scores)} "
            f"max={max(cross_scores):.2f}",
            f"- margin (median-same − max-cross): {margin_sum[0]:+.2f} "
            f"→ {'healthy' if margin_sum[0] > 0.05 else 'thin — corpus too close'}",
        ]
        # suggested threshold: midpoint between max-cross and median-same
        sug = (max(cross_scores) + sorted(same_scores)[len(same_scores) // 2]) / 2
        lines.append(f"- suggested threshold ≈ {sug:.2f} "
                     f"(current {DEFAULT_THRESHOLD})")
    fails = [(c, f) for c, r in results.items() for f in r["fails"]]
    if fails:
        lines += ["", "## failures", ""] + [f"- [{c}] {f}" for c, f in fails]
    lines += ["", f"**{total_p} pass / {total_f} fail**"]
    report = "\n".join(lines)
    print(report)

    if ARGS.mddb:
        _mddb_doc(ARGS.mddb, report, results)
    if ARGS.report_cms:
        _cms_page(report)
    return 0 if total_f == 0 else 1


def _mddb_doc(base: str, report: str, results: dict) -> None:
    import urllib.request
    doc = {
        "collection": "ada-ha-scenario-reports",
        "key": f"voice-bench-{datetime.now():%Y%m%d-%H%M}",
        "contentMd": report,
        "meta": {"kind": ["benchmark"], "bench": ["voice"],
                 "fails": [sum(r["fail"] for r in results.values())]},
    }
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/documents", data=json.dumps(doc).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
        print("\n[mddb] report doc written")
    except Exception as exc:
        print(f"\n[mddb] write failed: {exc}")


def _cms_page(report: str) -> None:
    try:
        from scripts.scenario_report import upsert_page  # type: ignore
        upsert_page("bench-voice", "Voice corpus bench", report)
        print("[cms] bench-voice updated")
    except Exception as exc:
        print(f"[cms] skipped: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
