"""Noise-gated input polish for typed/relay lanes (card micro-ai-input-polish).

Bench evidence (chaba scripts/micro-bench, reports/micro-polish 2026-10-11):
a strong polisher ahead of the text lane lifts exact tool-call match on
noisy inputs (typo/fragment/code-switch), but ungated it pays a ~56%
net-token tax for the same win. The deploy shape that kept the gain was
gate -> polish -> lang_preserve check (variant polish-pregate-gemini:
22/44 exact, same as always-polish, ~1/3 the token tax).

This module is that shape behind ADA_INPUT_POLISH=1 (off by default).
All failures fail open — the original text passes through untouched.
"(system) ..." notes and oversized input are never polished.

Gate signals mirror the bench's noise_dict: mixed Thai/Latin script,
Thai-dominant input, or an english-wordlist unknown-token ratio above
threshold. Honest gap (same as bench): real-word mishearings pass the
gate — that noise class needs ASR confidence text doesn't carry.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("tools")

_ENABLED = os.environ.get(
    "ADA_INPUT_POLISH", "").strip().lower() in ("1", "true", "yes")
_MODEL = os.environ.get("ADA_INPUT_POLISH_MODEL", "gemini-3.5-flash-lite")
_MAX_CHARS = int(os.environ.get("ADA_INPUT_POLISH_MAX_CHARS", "800"))
_TIMEOUT_S = float(os.environ.get("ADA_INPUT_POLISH_TIMEOUT_S", "4"))
_UNKNOWN_MIN = float(os.environ.get("ADA_INPUT_POLISH_UNKNOWN_RATIO", "0.15"))

_WORDS: frozenset | None = None

# Same prompt that won the bench (lib.POLISH_PROMPTS["normalize_v1"]).
_PROMPT = """\
You are an input normalizer for a voice assistant. Rewrite the user's
utterance into clean, canonical phrasing: fix ASR mishearings, typos,
code-switched Thai/English, and fragments. Rules:
- Preserve every name, number, entity id, and the user's intent EXACTLY.
- If the input is already clean, return it unchanged.
- If the input is ambiguous or gibberish, return it UNCHANGED — never
  invent a referent or guess a command.
- Reply in the same language as the input (Thai stays Thai).
- Output ONLY the rewritten utterance — no quotes, no explanation."""


def _wordlist() -> frozenset:
    global _WORDS
    if _WORDS is None:
        path = Path(__file__).parent / "en-common-words.txt"
        _WORDS = frozenset(path.read_text().split())
    return _WORDS


def _thai_ratio(text: str) -> float:
    chars = [c for c in text or "" if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if "฀" <= c <= "๿") / len(chars)


def should_polish(text: str) -> tuple[bool, str]:
    """Gate decision, pure and testable. Mirrors bench pre_gate_check."""
    if text.startswith("(system)"):
        return False, "system note"
    if len(text) > _MAX_CHARS:
        return False, "too long"
    tr = _thai_ratio(text)
    if 0.0 < tr <= 0.95 and re.search(r"[a-zA-Z]", text):
        return True, "mixed-script code-switch"
    if tr > 0.5:
        return True, "thai-dominant input"
    words = re.findall(r"[a-zA-Z']+", text)
    if not words:
        return False, "no latin words"
    wl = _wordlist()
    unk = sum(1 for w in words if w.lower() not in wl) / len(words)
    if unk > _UNKNOWN_MIN:
        return True, f"unknown-ratio {unk:.2f} > {_UNKNOWN_MIN}"
    return False, f"clean (unknown-ratio {unk:.2f})"


def _lang_preserved(raw: str, polished: str) -> bool:
    """Post-gate: reject translation drift (the observed weak-polish mode)."""
    r, p = _thai_ratio(raw), _thai_ratio(polished)
    if r < 0.05 and p > 0.15:
        return False
    if r > 0.5 and p < 0.2:
        return False
    return True


async def maybe_polish(text: str) -> str:
    """Return polished text when the gate selects it, else the original.

    Never raises — any failure (flag off, quota, timeout, drift) passes
    the raw message through so typed lanes are never worse than today.
    """
    try:
        if not _ENABLED:
            return text
        ok, reason = should_polish(text)
        if not ok:
            return text
        from backend import gemini_pool
        resp = await gemini_pool.generate(
            model=_MODEL,
            contents=f"{_PROMPT}\n\n{text}",
            timeout=_TIMEOUT_S,
            tool="input_polish")
        polished = (getattr(resp, "text", None) or "").strip()
        if not polished or not _lang_preserved(text, polished):
            logger.warning("input_polish: rejected polish (%s)", reason)
            return text
        logger.info("input_polish: polished (%s)", reason)
        return polished
    except Exception as exc:
        logger.warning("input_polish failed open: %s", exc)
        return text
