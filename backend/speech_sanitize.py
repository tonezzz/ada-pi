"""Scrub markup artifacts out of text headed for speech.

Also strips parenthesized internal annotations — "(ha_safety: caution)",
"(kanban list review)" — that the model voices as asides (session
67b02417a8, card ada-output-hygiene-tags).

Two consumers:

- The Gemini Live output-transcription stream (realtime_provider): the
  model occasionally speaks markup it picked up from CMS pages or memory —
  the transcript then shows literal "&nbsp;ชัดเจน" or "][พาสเจอร์ไรซ์]"
  (transcript 519088cb6d, 2026-10-04). The audio is already spoken, but
  sanitizing keeps the junk out of the stored transcript, conversation
  log, and anything distilled from them — so it is never parroted back.
- vcast_say / backend Gemini TTS (tool_runner): there the text is input
  to a synthesizer, so sanitizing prevents the artifact from being spoken
  at all.

``split_artifact_tail`` exists for the streaming case: a delta can end
mid-token ("&nbs", "[link", "]("), so the provider holds the possible
partial artifact and retries once the next delta completes it.
"""
from __future__ import annotations

import html
import re

# Only ;-terminated entities decode — html.unescape on bare text would
# also eat legacy forms mid-word ("&not that" -> "¬that", "&ampx" -> "&x").
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
# Unknown entities unescape() passes through — spoken text never legit
# produces "&word;", so drop them rather than show junk in the transcript.
_LEFTOVER_ENTITY_RE = re.compile(r"&[a-zA-Z][a-zA-Z0-9]{1,9};")
# A truncated/unfinished entity at the very end ("...&nbs"). Requires 2+
# chars after '&' so "R&D", "AT&T" at a fragment boundary survive.
_PARTIAL_ENTITY_END_RE = re.compile(r"&[#a-zA-Z][a-zA-Z0-9#]{1,9}$")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>\n]{0,80}>")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)\n]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)\n]*\)")
_MD_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]\n]*\]")
_MD_JUNCTION_RE = re.compile(r"\]\[|\]\(|\)\[")
# Parenthesized internal annotations the model speaks as asides —
# "(ha_safety: caution)", "(kanban list review)". The technical shape is
# the tell: >=2 all-lowercase [a-z0-9_] tokens joined by space/colon/
# comma inside parens. A lone word ("(draft)", "(system)") and
# natural-language asides with capitals ("(I think)") or Thai survive.
_INTERNAL_TAG_RE = re.compile(
    r"\(\s*[a-z][a-z0-9_]*(?:[\s:：,]+[a-z0-9_]+)+\s*\)")
# Same shape without the closing paren — a truncated annotation at the
# very end of a delta/turn ("(ha_safety: caution" ) still drops.
_INTERNAL_TAG_OPEN_RE = re.compile(
    r"\(\s*[a-z][a-z0-9_]*(?:[\s:：,]+[a-z0-9_]+)+\s*$")
_BRACKET_RE = re.compile(r"[\[\]]")
_MARKUP_CHARS_RE = re.compile(r"[`*]+")
_SPACE_RUN_RE = re.compile(r"[^\S\n]{2,}")

# Trailing fragment that could become an artifact once the next
# transcription delta arrives — held back instead of emitted/sanitized.
ARTIFACT_TAIL_RE = re.compile(
    r"(?:&[#a-zA-Z0-9]{0,9}"      # partial &entity;
    r"|!?\[[^\]\n]{0,80}"         # '[' / '![' + link text in progress
    r"|\]\([^)\n]{0,120}"         # '](url in progress'
    r"|\]\[[^\]\n]{0,60}"         # '][ref in progress'
    r"|\([^)\n]{0,80}"            # '(tag in progress' — internal annotation
    r"|[\]!]"                     # bare ']' or '!' — opener for ]( / ][ / ![
    r")$"
)


def sanitize_speech(text: str) -> str:
    """Return `text` with speakable markup artifacts removed."""
    if not text:
        return text
    # The observed leak verbatim, terminated or not.
    t = re.sub(r"&nbsp;?", " ", text, flags=re.IGNORECASE)
    t = _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), t)
    t = t.replace("\xa0", " ")
    t = _LEFTOVER_ENTITY_RE.sub("", t)
    t = _HTML_TAG_RE.sub("", t)
    t = _MD_IMAGE_RE.sub(r"\1", t)
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _MD_REF_LINK_RE.sub(r"\1", t)
    t = _INTERNAL_TAG_RE.sub(" ", t)
    t = _INTERNAL_TAG_OPEN_RE.sub("", t)
    t = _MD_JUNCTION_RE.sub(" ", t)
    t = _BRACKET_RE.sub("", t)
    t = _MARKUP_CHARS_RE.sub("", t)
    t = _PARTIAL_ENTITY_END_RE.sub("", t)
    return _SPACE_RUN_RE.sub(" ", t)


def split_artifact_tail(text: str) -> tuple[str, str]:
    """Split `text` into (safe-to-emit, hold-for-next-delta)."""
    m = ARTIFACT_TAIL_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start():]
