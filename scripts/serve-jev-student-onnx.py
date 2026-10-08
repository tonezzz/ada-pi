#!/usr/bin/env python3
"""serve-jev-student-onnx.py — ONNX lane twin of serve-jev-student.py.

Same /health + /v1/systemone contract as scripts/serve-jev-student.py,
but serves from an ONNX export with onnxruntime + tokenizers — no torch,
transformers, or fastapi. This is the Nest lane shim for hosts too small
(or too busy) for the torch path: tony-dell CPU/iGPU, edge boxes.

Model dir must contain: model.onnx, tokenizer.json, config.json
(config.json carries custom_params.noul_threshold through to /health).

Env:  JEV_ONNX  model dir (default ~/jev-student/onnx)
      JEV_HOST  bind address (default 127.0.0.1 — pass the host's
                tailscale IP to expose it as a lane endpoint)
      JEV_PORT  port (default 8878)
      JEV_PROVIDER  onnxruntime provider (default cpu; 'openvino:GPU'
                maps to OpenVINOExecutionProvider for the iGPU lane —
                needs the onnxruntime-openvino build)

CLI flags mirror the env vars and win over them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HOME = Path.home()
ap = argparse.ArgumentParser()
ap.add_argument("--model", default=os.environ.get(
    "JEV_ONNX", str(HOME / "jev-student" / "onnx")))
ap.add_argument("--host", default=os.environ.get("JEV_HOST", "127.0.0.1"))
ap.add_argument("--port", type=int,
                default=int(os.environ.get("JEV_PORT", "8878")))
ap.add_argument("--provider", default=os.environ.get("JEV_PROVIDER", "cpu"),
                help="cpu | openvino:GPU | openvino:CPU | cuda — lane runtime")
ARGS = ap.parse_args()

MODEL_DIR = Path(ARGS.model).expanduser()
TOK = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
TOK.enable_truncation(max_length=96)

_CFG = {}
try:
    _CFG = json.loads((MODEL_DIR / "config.json").read_text())
except Exception:
    pass
THR = float((_CFG.get("custom_params") or {}).get("noul_threshold", 0.70))


def _providers(spec: str):
    """Map a lane runtime name to onnxruntime providers."""
    if spec.startswith("openvino"):
        dev = spec.split(":", 1)[1] if ":" in spec else "CPU"
        return [("OpenVINOExecutionProvider", {"device_type": dev})]
    if spec == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


SO = ort.SessionOptions()
SO.intra_op_num_threads = int(os.environ.get("JEV_THREADS", "0") or 0)
SO.log_severity_level = 3
SESSION = ort.InferenceSession(str(MODEL_DIR / "model.onnx"), SO,
                               providers=_providers(ARGS.provider))
INPUT_NAMES = {i.name for i in SESSION.get_inputs()}
MODEL_NAME = _CFG.get("_name_or_path") or MODEL_DIR.name


def score(turn: str) -> tuple[float, int]:
    """p(LABEL_1) for a user turn + token count."""
    enc = TOK.encode(turn)
    feeds = {}
    if "input_ids" in INPUT_NAMES:
        feeds["input_ids"] = np.array([enc.ids], dtype=np.int64)
    if "attention_mask" in INPUT_NAMES:
        feeds["attention_mask"] = np.array(
            [enc.attention_mask], dtype=np.int64)
    if "token_type_ids" in INPUT_NAMES:
        feeds["token_type_ids"] = np.array(
            [enc.type_ids], dtype=np.int64)
    logits = SESSION.run(None, feeds)[0][0]
    e = np.exp(logits - logits.max())
    p = float((e / e.sum())[1])
    return p, len(enc.ids)


def turn_from_state(state) -> str:
    if isinstance(state, dict):
        state = json.dumps(state, ensure_ascii=False)
    if isinstance(state, list):
        state = " ".join(map(str, state))
    s = str(state)
    m = re.search(r'The user turn was:\s*"([^"]+)"', s)
    if m:
        return m.group(1)
    m = re.search(r'User said:\s*"([^"]+)"', s)
    if m:
        return m.group(1)
    return s[-300:]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet — the bench counts calls itself
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "model": MODEL_NAME,
                                    "thr": THR,
                                    "provider": ARGS.provider})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/systemone":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        t0 = time.time()
        state = body.get("state", "")
        questions = body.get("questions") or {}
        turn = turn_from_state(state)
        p, ntok = score(turn)
        answers = {}
        for qid, q in questions.items():
            qtype = (q or {}).get("type")
            if qtype == "noul":
                answers[qid] = {"type": "noul", "noul": p}
            elif qtype == "choice":
                answers[qid] = {"type": "choice", "choice": "answer",
                                "confidence": 0.0}
            else:
                answers[qid] = {"type": qtype or "unknown", "score": p}
        return self._send(200, {
            "model": MODEL_NAME,
            "answers": answers,
            "usage": {"input_tokens": ntok, "output_tokens": 0,
                      "elapsed_s": round(time.time() - t0, 3)}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"jev-student-onnx: {MODEL_NAME} on {ARGS.host}:{ARGS.port} "
          f"provider={ARGS.provider} thr={THR}", flush=True)
    srv.serve_forever()
