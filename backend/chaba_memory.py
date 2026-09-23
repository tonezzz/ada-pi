"""chaba_memory.py — file-backed memory for CHABA_MEMORY=1 guest instances.

When enabled, the service skips MDDB/NotebookLM entirely and uses the chaba
file store instead:

    <dir>/context-guest.md       rendered shared context (system prompt)
    <dir>/report-guest.yml       pull-only detail report (guest_recall)
    <dir>/guests/<name>.yml      public memories, declared-name namespace
    <dir>/users/<name>.yml       public memories after admin promotion
    <dir>/users/<name>-private.yml   private namespace earned on promotion
    <dir>/pending/<name>.yml     registrations awaiting admin approval

All access is string-level YAML — no embeddings, no AI, no network.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("chaba_memory")

DEFAULT_DIR = "~/.local/share/chaba"


def enabled() -> bool:
    return os.environ.get("CHABA_MEMORY", "").lower() in ("1", "true", "yes")


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "anon"


class ChabaMemory:
    """File-backed guest/user memory store. All paths stay under `root`."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(os.path.expanduser(root or os.environ.get("CHABA_DIR", DEFAULT_DIR)))
        # session_id -> {"kind": "guest"|"user", "name": str}
        self.sessions: dict[str, dict[str, str]] = {}

    # ---------- paths ----------

    def _path(self, *parts: str) -> Path:
        p = (self.root / Path(*parts)).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError("path escapes chaba dir")
        return p

    def guest_file(self, name: str) -> Path:
        return self._path("guests", f"{_slug(name)}.yml")

    def user_file(self, name: str) -> Path:
        return self._path("users", f"{_slug(name)}.yml")

    def private_file(self, name: str) -> Path:
        return self._path("users", f"{_slug(name)}-private.yml")

    def pending_file(self, name: str) -> Path:
        return self._path("pending", f"{_slug(name)}.yml")

    # ---------- identity ----------

    def identity(self, session_id: str | None) -> dict[str, str]:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        return {"kind": "guest", "name": ""}

    def set_identity(self, session_id: str, kind: str, name: str) -> None:
        self.sessions[session_id] = {"kind": kind, "name": name}

    def memory_file_for(self, session_id: str | None) -> Path:
        ident = self.identity(session_id)
        if ident["kind"] == "user" and ident["name"]:
            return self.user_file(ident["name"])
        return self.guest_file(ident["name"] or "anon")

    # ---------- context ----------

    def system_context(self, session_id: str | None = None) -> str:
        """Shared guest context + private namespace when the session belongs
        to a promoted user."""
        parts = []
        ctx = self._path("context-guest.md")
        if ctx.exists():
            parts.append(ctx.read_text(encoding="utf-8", errors="replace"))
        ident = self.identity(session_id)
        if ident["kind"] == "user" and ident["name"]:
            priv = self.private_file(ident["name"])
            if priv.exists():
                parts.append(f"## private notes for {ident['name']}\n\n"
                             + priv.read_text(encoding="utf-8", errors="replace"))
        return "\n\n".join(parts)

    # ---------- writes ----------

    def _load_doc(self, path: Path) -> dict:
        if not path.exists():
            return {"entries": []}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {"entries": []}
        except Exception:
            return {"entries": []}

    def _save_doc(self, path: Path, doc: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        tmp.replace(path)

    def remember(self, session_id: str | None, key: str, text: str) -> dict[str, Any]:
        """Append a public memory under the session's declared name."""
        ident = self.identity(session_id)
        name = ident["name"] or "anon"
        path = self.memory_file_for(session_id)
        key = _slug(key)[:60] or "note"
        doc = self._load_doc(path)
        doc.setdefault("name", name)
        doc.setdefault("entries", [])
        doc["entries"] = [e for e in doc["entries"] if e.get("key") != key]
        doc["entries"].append({
            "key": key,
            "text": text[:2000],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "public": True,
        })
        self._save_doc(path, doc)
        logger.info("remember %s/%s for session=%s", name, key, session_id)
        return {"ok": True, "file": path.name, "key": key, "public": True,
                "scope": ident["kind"]}

    def remember_private(self, session_id: str | None, key: str, text: str) -> dict[str, Any]:
        """Write to a promoted user's private namespace. Guests get denied."""
        ident = self.identity(session_id)
        if ident["kind"] != "user" or not ident["name"]:
            raise PermissionError("private memory requires a promoted user session")
        path = self.private_file(ident["name"])
        doc = self._load_doc(path)
        doc.setdefault("name", ident["name"])
        doc.setdefault("entries", [])
        key = _slug(key)[:60] or "note"
        doc["entries"] = [e for e in doc["entries"] if e.get("key") != key]
        doc["entries"].append({
            "key": key, "text": text[:2000],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "public": False,
        })
        self._save_doc(path, doc)
        return {"ok": True, "file": path.name, "key": key, "public": False}

    # ---------- recall ----------

    def _iter_memories(self, include_private_for: str | None = None):
        for pattern in ("guests/*.yml", "users/*.yml"):
            for p in sorted(self.root.glob(pattern)):
                if p.name.endswith("-private.yml") and _slug(include_private_for or "") != p.name[:-12]:
                    continue
                doc = self._load_doc(p)
                for e in doc.get("entries", []):
                    if isinstance(e, dict):
                        yield doc.get("name", p.stem), e

    def recall(self, query: str, session_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Case-insensitive substring search over public memory files (+ the
        session user's private file). String-level only."""
        q = (query or "").lower().strip()
        if not q:
            return []
        ident = self.identity(session_id)
        priv_for = ident["name"] if ident["kind"] == "user" else None
        hits = []
        terms = q.split()
        for name, e in self._iter_memories(priv_for):
            hay = f"{e.get('key','')} {e.get('text','')}".lower()
            score = sum(1 for t in terms if t in hay)
            if q in hay:
                score += len(terms)
            if score:
                hits.append({"name": name, "key": e.get("key"), "text": e.get("text"),
                             "at": e.get("at"), "score": score})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    # ---------- registration / promotion ----------

    def register_pending(self, name: str, session_id: str | None = None,
                         extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a guest's name (+voice meta) awaiting admin promotion."""
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        doc = {
            "name": name,
            "slug": _slug(name),
            "status": "pending",
            "session_id": session_id,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **(extra or {}),
        }
        self._save_doc(self.pending_file(name), doc)
        # Claim the name for this session immediately — memory writes before
        # promotion already land under guests/<name>.yml.
        if session_id:
            self.set_identity(session_id, "guest", name)
        logger.info("pending registration: %s (session=%s)", name, session_id)
        return {"ok": True, "status": "pending", "name": name}

    def pending_list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("pending/*.yml")):
            doc = self._load_doc(p)
            if doc.get("status") == "pending":
                out.append(doc)
        return out

    def promote(self, name: str, ha_person: str | None = None) -> dict[str, Any]:
        """Admin promotes a pending guest to a named user.

        Moves guests/<name>.yml -> users/<name>.yml, creates the private
        namespace, clears the pending marker, and upgrades any live session
        bound to that name.
        """
        slug = _slug(name)
        pend = self.pending_file(name)
        doc = self._load_doc(pend)
        doc["status"] = "promoted"
        doc["promoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        doc["ha_person"] = ha_person or f"person.{slug}"
        self._save_doc(pend, doc)

        gfile, ufile = self.guest_file(name), self.user_file(name)
        if gfile.exists():
            ufile.parent.mkdir(parents=True, exist_ok=True)
            gfile.replace(ufile)
        elif not ufile.exists():
            self._save_doc(ufile, {"name": doc.get("name", name), "entries": []})

        priv = self.private_file(name)
        if not priv.exists():
            self._save_doc(priv, {"name": doc.get("name", name),
                                  "private": True, "entries": []})

        # Upgrade live sessions that registered under this name.
        upgraded = []
        for sid, ident in self.sessions.items():
            if _slug(ident.get("name", "")) == slug:
                self.set_identity(sid, "user", doc.get("name", name))
                upgraded.append(sid)
        logger.info("promoted %s -> user (ha_person=%s, sessions=%s)",
                    name, doc["ha_person"], upgraded)
        return {"ok": True, "name": doc.get("name", name), "slug": slug,
                "ha_person": doc["ha_person"], "sessions": upgraded}

    def health(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "dir": str(self.root),
            "context_present": self._path("context-guest.md").exists(),
            "guests": len(list(self.root.glob("guests/*.yml"))),
            "users": len([p for p in self.root.glob("users/*.yml")
                          if not p.name.endswith("-private.yml")]),
            "pending": len(self.pending_list()),
        }


_INSTANCE: ChabaMemory | None = None


def get_store() -> ChabaMemory:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ChabaMemory()
    return _INSTANCE
