"""Decision-check pipeline — verify a purchase/listing before buying.

Stages: normalize -> criteria -> verify -> persist -> event.

Input is deliberately capture-based (pasted text, screenshot bytes, or a
URL) because marketplace pages like Shopee are hostile to scraping. A
screenshot or share-sheet text carries the live price/rating/sold count
better than any fetcher we could run.

Generic contract, 'shopee' adapter first: adapters contribute detection
and prompt hints; everything downstream (criteria, verdict schema,
persistence, events) is domain-neutral so other marketplaces/decision
types plug in later.

Persistence lands in the `purchase` memory bank's MDDB collection:
criteria docs (kind fact/preference) teach Ada the user's rules; check
docs (kind check) are the audit log and answer "didn't I check this
before?" through ordinary bank search.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from google import genai
from google.genai import types

from backend.mddb_client import MddbClient
from backend.memory_banks import MemoryBankRegistry, _meta_first, _slug

logger = logging.getLogger("decision.check")

DECISION_MODEL = os.environ.get("DECISION_MODEL", "gemini-2.5-flash")
DECISION_BANK = os.environ.get("DECISION_BANK", "purchase")
DECISION_TIMEOUT_S = float(os.environ.get("DECISION_TIMEOUT_S", "90"))
# chaba-admin Events feed — emitted through the Home Assistant
# shell_command.chaba_event service (base64 JSON -> chaba-event-log.py),
# so no extra credentials or SSH are needed beyond HOME_ASSISTANT_*.

PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "brand": {"type": "string"},
        "model": {"type": "string"},
        "price": {"type": "number"},
        "currency": {"type": "string"},
        "shop_name": {"type": "string"},
        "shop_rating": {"type": "number"},
        "review_count": {"type": "integer"},
        "sold_count": {"type": "integer"},
        "marketplace": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["name", "marketplace"],
    "additionalProperties": False,
}

VERDICT_SCHEMA_HINT = """{
  "verdict": "buy" | "caution" | "avoid",
  "confidence": 0.0-1.0,
  "summary": "one short paragraph — what this is, verdict, why",
  "reasons": ["supporting reason", ...],
  "flags": ["red flag or risk", ...],
  "price_assessment": "fair | high | low | unknown — vs. typical market price",
  "price_reference": "what the market price looks like, if known",
  "criteria_results": [{"criterion": "...", "pass": true|false, "note": "..."}],
  "alternatives": [{"name": "...", "source": "...", "price": "...", "why": "..."}]
}"""


@dataclass
class Adapter:
    """Domain adapter: detection + prompt hints for one decision domain."""

    name: str
    detect_patterns: list[str]
    normalize_hints: str
    verify_hints: str

    def matches(self, url: str, text: str) -> bool:
        hay = f"{url} {text}".lower()
        return any(p in hay for p in self.detect_patterns)


ADAPTERS = {
    "shopee": Adapter(
        name="shopee",
        detect_patterns=["shopee."],
        normalize_hints=(
            "This is a Shopee marketplace listing. Shopee pages show the product "
            "name, price (usually THB ฿, may show a range like ฿100-฿200 — take "
            "the lowest), shop name, shop rating out of 5, number of ratings/"
            "reviews, and sold count. Extract what is visible."
        ),
        verify_hints=(
            "Shopee-specific red flags: price far below other listings for the "
            "same item, new shop with few ratings, no 'Shopee Mall' or "
            "'Preferred' badge on branded goods, stock/catalog photos only, "
            "suspiciously perfect 5.0 rating with very few reviews, listings "
            "that hide the real price behind a variant. Cross-check the claimed "
            "brand/model against the manufacturer's official specs — fake or "
            "misrepresented specs are the most common Shopee failure mode. "
            "Compare the price against the official store, Lazada, and other "
            "Shopee sellers."
        ),
    ),
}

GENERIC_ADAPTER = Adapter(
    name="generic",
    detect_patterns=[],
    normalize_hints=(
        "Identify the product or item being considered. Extract name, brand, "
        "model, price, currency, seller/shop, ratings, and URL if present."
    ),
    verify_hints=(
        "Check for common purchase red flags, verify claimed specs against "
        "manufacturer information, and compare the price against typical "
        "market prices."
    ),
)


def _detect_adapter(url: str, text: str) -> Adapter:
    for adapter in ADAPTERS.values():
        if adapter.matches(url, text):
            return adapter
    return GENERIC_ADAPTER


def _extract_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction — grounding calls return prose around JSON."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, depth = text.find("{"), 0
        if start >= 0:
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        text = text[start : i + 1]
                        break
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response JSON is not an object")
    return data


@dataclass
class CheckResult:
    ok: bool
    verdict: str = ""
    confidence: float = 0.0
    summary: str = ""
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    price_assessment: str = ""
    price_reference: str = ""
    criteria_results: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    product: dict[str, Any] = field(default_factory=dict)
    mode: str = "quick"
    adapter: str = "generic"
    durations_ms: dict[str, int] = field(default_factory=dict)
    doc_key: str | None = None
    persisted: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "summary": self.summary,
            "reasons": self.reasons,
            "flags": self.flags,
            "price_assessment": self.price_assessment,
            "price_reference": self.price_reference,
            "criteria_results": self.criteria_results,
            "alternatives": self.alternatives,
            "product": self.product,
            "mode": self.mode,
            "adapter": self.adapter,
            "durations_ms": self.durations_ms,
            "doc_key": self.doc_key,
            "persisted": self.persisted,
            "error": self.error,
        }


class DecisionCheckEngine:
    """Runs the normalize -> criteria -> verify -> persist pipeline."""

    def __init__(
        self,
        mddb: MddbClient,
        registry: MemoryBankRegistry,
        instance: str,
        client: Any = None,
        ha_client: Any = None,
    ) -> None:
        self.mddb = mddb
        self.registry = registry
        self.instance = instance
        self.ha_client = ha_client
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = os.environ.get("DECISION_MODEL", DECISION_MODEL)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def check(
        self,
        text: str | None = None,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        url: str | None = None,
        mode: str = "quick",
        caller: str = "api",
    ) -> CheckResult:
        started = time.monotonic()
        durations: dict[str, int] = {}
        mode = "deep" if mode == "deep" else "quick"
        text = (text or "").strip()
        url = (url or "").strip()
        if not text and not image and not url:
            raise ValueError("provide text, image, or url")

        adapter = _detect_adapter(url, text)
        result = CheckResult(ok=False, mode=mode, adapter=adapter.name,
                             durations_ms=durations)

        # --- stage 1: normalize input into a product record ---
        t = time.monotonic()
        product = await self._normalize(text, image, image_mime, url, adapter)
        durations["normalize_ms"] = int((time.monotonic() - t) * 1000)
        result.product = product

        # --- stage 2: load stored criteria + relevant history ---
        t = time.monotonic()
        criteria = await self._load_criteria(product)
        durations["criteria_ms"] = int((time.monotonic() - t) * 1000)

        # --- stage 3: verify against the web + criteria ---
        t = time.monotonic()
        verdict = await self._verify(product, criteria, adapter, mode)
        durations["verify_ms"] = int((time.monotonic() - t) * 1000)
        result.verdict = str(verdict.get("verdict") or "")
        result.confidence = max(0.0, min(1.0, float(verdict.get("confidence") or 0)))
        result.summary = str(verdict.get("summary") or "")
        result.reasons = [str(r) for r in verdict.get("reasons") or []]
        result.flags = [str(f) for f in verdict.get("flags") or []]
        result.price_assessment = str(verdict.get("price_assessment") or "")
        result.price_reference = str(verdict.get("price_reference") or "")
        result.criteria_results = [
            c for c in verdict.get("criteria_results") or [] if isinstance(c, dict)
        ]
        result.alternatives = [
            a for a in verdict.get("alternatives") or [] if isinstance(a, dict)
        ]
        result.ok = result.verdict in {"buy", "caution", "avoid"}

        # --- stage 4: persist + event (best-effort, never fails the check) ---
        t = time.monotonic()
        result.doc_key, result.persisted = await self._persist(result, caller)
        durations["persist_ms"] = int((time.monotonic() - t) * 1000)
        durations["total_ms"] = int((time.monotonic() - started) * 1000)
        await self._emit_event(result)
        return result

    async def _normalize(
        self,
        text: str,
        image: bytes | None,
        image_mime: str,
        url: str,
        adapter: Adapter,
    ) -> dict[str, Any]:
        parts: list[types.Part] = [
            types.Part.from_text(text=(
                "Extract the product listing details into JSON. "
                f"{adapter.normalize_hints} "
                "If a field is not visible, omit it. "
                "marketplace is the detected site ('shopee', 'lazada', 'other')."
                + (f"\nListing URL: {url}" if url else "")
                + (f"\nCaptured listing text:\n{text[:8000]}" if text else "")
            )),
        ]
        if image:
            parts.append(types.Part.from_bytes(data=image, mime_type=image_mime))
        response = await asyncio.wait_for(
            self.client.aio.models.generate_content(
                model=self.model,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=PRODUCT_SCHEMA,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            ),
            timeout=DECISION_TIMEOUT_S,
        )
        return _extract_json(response.text)

    async def _load_criteria(self, product: dict[str, Any]) -> list[str]:
        """Active non-check docs in the bank = the user's criteria corpus."""
        try:
            bank = self.registry.bank(DECISION_BANK)
        except KeyError as exc:
            logger.warning("decision criteria bank unavailable: %s", exc)
            return []
        docs = await self.mddb.search_documents(
            collection=bank.mddb_collection,
            filter_meta={"status": ["active"]},
            limit=50,
        )
        criteria = []
        for doc in docs or []:
            meta = doc.get("meta") or {}
            if _meta_first(meta, "kind") == "check":
                continue  # history log, not a rule
            content = str(doc.get("contentMd") or doc.get("content_md") or "").strip()
            if content:
                criteria.append(content)
        return criteria

    async def _verify(
        self,
        product: dict[str, Any],
        criteria: list[str],
        adapter: Adapter,
        mode: str,
    ) -> dict[str, Any]:
        criteria_block = (
            "User's purchase criteria (apply each one and report pass/fail):\n"
            + "\n".join(f"- {c}" for c in criteria)
            if criteria
            else "No stored user criteria — apply generic due diligence only."
        )
        prompt = (
            "You are a careful purchase-verification assistant. A user is "
            "considering buying this item — verify it against the live web "
            "before they pay.\n\n"
            f"Product record (from the listing):\n{json.dumps(product, ensure_ascii=False, indent=1)}\n\n"
            f"{adapter.verify_hints}\n\n"
            f"{criteria_block}\n\n"
            + (
                "DEEP mode: search thoroughly. Verify the brand/model exists, "
                "cross-check the claimed specs against the manufacturer, look "
                "for reviews and known defects, compare prices across at least "
                "2-3 sources, and find up to 4 real alternatives (name, source, "
                "approximate price, why it's worth considering).\n\n"
                if mode == "deep"
                else "QUICK mode: one focused check — obvious red flags, price "
                "sanity, and at most 2 alternatives if clearly better options "
                "exist.\n\n"
            )
            + "Reply with ONLY a JSON object matching:\n"
            + VERDICT_SCHEMA_HINT
        )
        response = await asyncio.wait_for(
            self.client.aio.models.generate_content(
                model=self.model,
                contents=[types.Part.from_text(text=prompt)],
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            ),
            timeout=DECISION_TIMEOUT_S * (2 if mode == "deep" else 1),
        )
        return _extract_json(response.text)

    async def _persist(self, result: CheckResult, caller: str) -> tuple[str | None, bool]:
        """Append the check to the bank collection as a kind=check doc."""
        try:
            bank = self.registry.bank(DECISION_BANK)
        except KeyError:
            return None, False
        now = datetime.now(timezone.utc)
        name = str(result.product.get("name") or "item")
        key = f"check/{now.strftime('%Y%m%d-%H%M%S')}-{_slug(name, 4)}"
        lines = [
            f"# Purchase check: {name}",
            "",
            f"- verdict: **{result.verdict}** (confidence {result.confidence:.2f})",
            f"- mode: {result.mode} · adapter: {result.adapter}",
            f"- checked: {now.isoformat(timespec='seconds')}",
        ]
        if result.product.get("price"):
            lines.append(
                f"- price: {result.product.get('price')} {result.product.get('currency', '')}"
            )
        for field_name in ("shop_name", "shop_rating", "url", "brand", "model"):
            value = result.product.get(field_name)
            if value:
                lines.append(f"- {field_name}: {value}")
        if result.price_assessment:
            lines.append(f"- price assessment: {result.price_assessment} — {result.price_reference}")
        if result.flags:
            lines.append("- flags: " + "; ".join(result.flags))
        lines += ["", result.summary]
        if result.alternatives:
            lines.append("\n## Alternatives")
            for alt in result.alternatives:
                lines.append(
                    f"- {alt.get('name', '?')} — {alt.get('source', '')} "
                    f"{alt.get('price', '')}: {alt.get('why', '')}"
                )
        meta = {
            "bank": [bank.name],
            "kind": ["check"],
            "scope": ["shared" if bank.scope == "shared" else self.instance],
            "status": ["active"],
            "valid_from": [now.date().isoformat()],
            "last_verified": [now.date().isoformat()],
            "source": ["api"],
            "written_by": ["decision_check"],
            "subject": [f"check-{_slug(name, 4)}"],
            "attribute": ["verdict"],
            "verdict": [result.verdict],
            "product": [name[:120]],
            "mode": [result.mode],
            "checked_by": [caller],
        }
        written = await self.mddb.add_document(
            bank.mddb_collection, key, "en", "\n".join(lines), meta,
            timeout=90,
        )
        if written is None:
            # A client-side timeout can still complete server-side (embedding
            # is slow) — confirm before reporting the write as lost, or a
            # retry would duplicate the doc.
            written = await self.mddb.get_document(bank.mddb_collection, key)
        return (key if written is not None else None, written is not None)

    async def _emit_event(self, result: CheckResult) -> None:
        """Best-effort chaba-admin Events feed entry via the HA
        shell_command.chaba_event service (never raises).

        Uses the shared ha_client's minted access token — the env
        HOME_ASSISTANT_TOKEN is a refresh token that REST rejects."""
        if self.ha_client is None or not getattr(self.ha_client, "token", None):
            return
        name = str(result.product.get("name") or "item")[:80]
        severity = {"buy": "info", "caution": "warn", "avoid": "fail"}.get(
            result.verdict, "info"
        )
        payload = base64.b64encode(json.dumps({
            "title": f"purchase check: {result.verdict or 'error'} — {name}",
            "category": "decision-check",
            "source": "ada-pi",
            "severity": severity,
            "requires_response": result.verdict == "avoid",
            "confidence": result.confidence,
            "body": result.summary[:600],
        }).encode()).decode()
        try:
            await self.ha_client._ensure_access_token()
            base = str(self.ha_client.base_url).rstrip("/")
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base}/api/services/shell_command/chaba_event",
                    headers={"Authorization": f"Bearer {self.ha_client._access_token}"},
                    json={"payload": payload},
                )
                if resp.status_code >= 400:
                    logger.debug("chaba event emit %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.debug("chaba event emit skipped: %s", exc)

    async def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first list of past checks from the bank collection."""
        try:
            bank = self.registry.bank(DECISION_BANK)
        except KeyError:
            return []
        docs = await self.mddb.search_documents(
            collection=bank.mddb_collection,
            filter_meta={"kind": ["check"], "status": ["active"]},
            limit=max(1, min(100, int(limit))),
        )
        out = []
        for doc in sorted(
            docs or [], key=lambda d: str(d.get("key") or ""), reverse=True
        ):
            meta = doc.get("meta") or {}
            if _meta_first(meta, "kind") != "check":
                continue
            out.append({
                "key": doc.get("key"),
                "product": _meta_first(meta, "product"),
                "verdict": _meta_first(meta, "verdict"),
                "mode": _meta_first(meta, "mode"),
                "checked_at": _meta_first(meta, "valid_from"),
            })
        return out


def decode_image(payload: dict[str, Any]) -> tuple[bytes | None, str]:
    """Accept image as base64 in JSON ('image_b64' + 'image_mime')."""
    raw = payload.get("image_b64")
    if not raw:
        return None, "image/jpeg"
    data = base64.b64decode(str(raw), validate=True)
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("image too large (15MB max)")
    mime = str(payload.get("image_mime") or "image/jpeg")
    if not mime.startswith("image/"):
        raise ValueError("image_mime must be image/*")
    return data, mime
