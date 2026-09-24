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
import subprocess
import sys
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
        if name:
            self._stamp_seen(kind, name)

    def _stamp_seen(self, kind: str, name: str) -> None:
        """Presence record: first_seen/last_seen on the user/guest file —
        creates the file on first contact so 'who appeared when' is
        answerable from the store, not the journal."""
        path = self.user_file(name) if kind == "user" else self.guest_file(name)
        today = time.strftime("%Y-%m-%d")
        doc = self._load_doc(path)
        doc.setdefault("name", name)
        doc.setdefault("entries", [])
        doc.setdefault("first_seen", today)
        doc["last_seen"] = today
        self._save_doc(path, doc)

    def memory_file_for(self, session_id: str | None) -> Path:
        ident = self.identity(session_id)
        if ident["kind"] == "user" and ident["name"]:
            return self.user_file(ident["name"])
        return self.guest_file(ident["name"] or "anon")

    # ---------- context ----------

    def _refresh_rendered_context(self) -> None:
        """Best-effort re-render of context-guest.md so each session starts
        with current memory files. Local string rendering only — skipped
        silently when the script is absent."""
        cmd = os.environ.get("CHABA_RENDER_CMD")
        if cmd is None:
            script = Path.home() / "CascadeProjects/chaba/scripts/chaba/render-memory.py"
            cmd = f"{sys.executable} {script} --profile guest" if script.exists() else ""
        if not cmd:
            return
        try:
            subprocess.run(
                cmd, shell=True, timeout=15,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.info("context-guest re-render skipped: %s", exc)

    def _session_log_entries(self, keep: int = 2, max_chars: int = 1200) -> list[str]:
        """Last `keep` entries from the rolling session log ('## ' split)."""
        path = self._path("session-memory.md")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        entries = [e.strip() for e in re.split(r"\n(?=## )", text) if e.strip()]
        tail = entries[-keep:]
        while tail and sum(len(e) for e in tail) > max_chars:
            tail = tail[1:]
        return tail

    def append_session_log(self, session_id: str | None, tail: str) -> None:
        """Append a trimmed conversation tail to the rolling session log so
        the next session can reference it. Entries: '## <ts> — <kind>:<name>'."""
        tail = tail.strip()
        if not tail:
            return
        ident = self.identity(session_id)
        label = f"{ident['kind']}:{ident['name'] or 'anon'}"
        path = self._path("session-memory.md")
        try:
            old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        except OSError:
            old = ""
        ts = time.strftime("%Y-%m-%d %H:%M")
        text = (old + f"\n\n## {ts} — {label}\n{tail}\n").lstrip()
        # Bound the file: keep the newest 30 '## ' entries.
        entries = [e for e in re.split(r"\n(?=## )", text) if e.strip()]
        if len(entries) > 30:
            text = "\n\n".join(entries[-30:])
        path.write_text(text, encoding="utf-8")
        logger.info("session log appended for %s (%d chars)", label, len(tail))

    def system_context(self, session_id: str | None = None) -> str:
        """Shared guest context + the session's own saved memories + recent
        session log + private namespace when the session belongs to a
        promoted user."""
        self._refresh_rendered_context()
        parts = []
        ctx = self._path("context-guest.md")
        if ctx.exists():
            parts.append(ctx.read_text(encoding="utf-8", errors="replace"))
        ident = self.identity(session_id)
        if ident["name"]:
            mine = self._load_doc(self.memory_file_for(session_id))
            entries = [e.get("text") for e in mine.get("entries", []) if e.get("text")]
            if entries:
                parts.append(
                    f"## memories saved by {ident['name']}\n\n"
                    + "\n".join(f"- {t}" for t in entries[-20:])
                )
        recent = self._session_log_entries()
        if recent:
            parts.append("## recent conversations\n\n" + "\n\n".join(recent))
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
