"""Memory-bank registry for Ada.

Loads the rendered bank registry (JSON produced from
chaba/docs/ssot/apps/ssot.apps.ada-memory-banks.yml), keeps only banks
assigned to this ADA_INSTANCE_ID, expands ``{instance}`` in collection
names, and resolves each bank's optional NotebookLM notebook.

Fail-fast: a bank assigned to this instance that cannot resolve a
collection is skipped loudly — logged and recorded in ``errors`` so
/api/health can flag the service as degraded. It is never silently
routed to another collection. A missing default registry file simply
disables the feature (banks were never deployed); an explicitly
configured ADA_MEMORY_BANKS_FILE that is missing or invalid is an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.instance import ada_instance_id

logger = logging.getLogger("memory.banks")

DEFAULT_REGISTRY_PATH = os.path.expanduser("~/.config/ada/memory-banks.json")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _load_notebook_ids() -> dict[str, str]:
    try:
        return json.loads(os.environ.get("NOTEBOOKLM_NOTEBOOK_IDS_JSON", "") or "{}")
    except json.JSONDecodeError:
        return {}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, max_words: int = 6) -> str:
    """Derive a doc-key slug from free text ('The gate remote!' -> 'the-gate-remote')."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return "-".join(slug.split("-")[:max_words]) or "note"


def _meta_first(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    if isinstance(value, list):
        return str(value[0]) if value and value[0] else None
    return str(value) if value else None


def doc_effective_status(doc: dict[str, Any], today: str | None = None) -> str:
    """A doc's status with lazy expiry applied: meta status stays 'active',
    but a passed valid_until reports 'expired' at recall time."""
    meta = doc.get("meta") or {}
    status = _meta_first(meta, "status") or "active"
    if status == "active":
        valid_until = _meta_first(meta, "valid_until")
        if valid_until:
            today = today or datetime.now(timezone.utc).date().isoformat()
            if valid_until < today:  # ISO dates compare lexically
                return "expired"
    return status


@dataclass
class MemoryBank:
    name: str
    title: str
    description: str
    scope: str  # shared | instance | person
    mddb_collection: str
    notebooklm_group: str | None
    notebooklm_id: str | None
    kinds: list[str]
    writable: bool
    write_policy: str  # confirmed | direct
    allowed_tools: list[str]
    status: str
    person_scope: str | None = None  # "default" for instance owner, "person.<id>" for person-scoped
    prompt_hidden: bool = False  # excluded from {writable_banks} in tool schemas — callable but never suggested

    def notebook(self, notebook_ids: dict[str, str]) -> str | None:
        """Resolve the deep-tier notebook id: literal id wins, else group."""
        if self.notebooklm_id:
            return self.notebooklm_id
        if self.notebooklm_group:
            return notebook_ids.get(self.notebooklm_group)
        return None


class MemoryBankRegistry:
    def __init__(
        self,
        path: str | None = None,
        instance: str | None = None,
        notebook_ids: dict[str, str] | None = None,
    ) -> None:
        explicit = os.environ.get("ADA_MEMORY_BANKS_FILE")
        self.path = path or explicit or DEFAULT_REGISTRY_PATH
        self.instance = instance or ada_instance_id()
        self.notebook_ids = notebook_ids if notebook_ids is not None else _load_notebook_ids()
        self.errors: list[str] = []
        self.schema_fields: set[str] = set()
        self._banks: dict[str, MemoryBank] = {}
        self.person_policies: dict[str, dict[str, Any]] = {}
        self.persona: dict[str, Any] = {}
        self._load(explicit=bool(path or explicit))

    def _load(self, explicit: bool) -> None:
        try:
            data = json.loads(Path(self.path).read_text())
        except FileNotFoundError:
            if explicit:
                self._error(f"registry file not found: {self.path}")
            else:
                logger.info("no memory-bank registry at %s — banks disabled", self.path)
            return
        except (OSError, json.JSONDecodeError) as exc:
            self._error(f"cannot load registry {self.path}: {exc}")
            return
        banks = data.get("banks", data) if isinstance(data, dict) else {}
        self.person_policies = (
            data.get("person_policies") if isinstance(data, dict) else None
        ) or {}
        self.persona = (
            data.get("persona") if isinstance(data, dict) else None
        ) or {}
        if isinstance(data, dict) and isinstance(data.get("schema"), dict):
            self.schema_fields = set(data["schema"].get("fields") or {})
        if not isinstance(banks, dict):
            self._error(f"registry {self.path}: 'banks' is not a map")
            return
        for name, spec in banks.items():
            self._add(name, spec if isinstance(spec, dict) else {})

    def _error(self, msg: str) -> None:
        self.errors.append(msg)
        logger.error("memory-bank registry: %s", msg)

    def _add(self, name: str, spec: dict[str, Any]) -> None:
        instances = spec.get("instances") or []
        if self.instance not in instances:
            return  # not assigned to this instance — silently skipped by design
        collection = str(spec.get("mddb_collection") or "").replace(
            "{instance}", self.instance
        )
        if not _NAME_RE.match(name):
            self._error(f"bank {name!r}: invalid bank name")
            return
        if not collection or not _NAME_RE.match(collection):
            self._error(f"bank {name!r}: unresolvable mddb_collection {collection!r}")
            return
        scope = str(spec.get("scope") or "shared")
        if (
            scope == "instance"
            and len(instances) > 1
            and "{instance}" not in str(spec.get("mddb_collection") or "")
        ):
            # A multi-instance bank must expand {instance}; otherwise both
            # instances would share one collection and leak private notes.
            # A single-instance bank may legitimately hardcode the name.
            self._error(
                f"bank {name!r}: scope=instance across {len(instances)} instances "
                f"but mddb_collection lacks {{instance}}"
            )
            return
        self._banks[name] = MemoryBank(
            name=name,
            title=str(spec.get("title") or name),
            description=str(spec.get("description") or ""),
            scope=scope,
            mddb_collection=collection,
            notebooklm_group=spec.get("notebooklm_group") or None,
            notebooklm_id=spec.get("notebooklm_id") or None,
            kinds=list(spec.get("kinds") or ["note"]),
            writable=bool(spec.get("writable")),
            write_policy=str(spec.get("write_policy") or "confirmed"),
            allowed_tools=list(spec.get("allowed_tools") or []),
            status=str(spec.get("status") or "planned"),
            person_scope=spec.get("person_scope") or None,
            prompt_hidden=bool(spec.get("prompt_hidden")),
        )

    @property
    def configured(self) -> bool:
        return bool(self._banks)

    def banks(self) -> dict[str, MemoryBank]:
        return dict(self._banks)

    def bank(self, name: str) -> MemoryBank:
        try:
            return self._banks[str(name).strip()]
        except KeyError:
            available = ", ".join(sorted(self._banks)) or "none"
            raise KeyError(
                f"unknown or unassigned memory bank {name!r} "
                f"(available for {self.instance}: {available})"
            ) from None

    def personal_bank_name(self, person_entity: str | None) -> str:
        """Resolve the effective personal bank name for a speaker.

        If a person-scoped bank exists whose person_scope matches the
        speaker's HA person entity, return that bank's name. Otherwise
        return the default 'personal' bank (the instance owner's).
        """
        if person_entity:
            for name, b in self._banks.items():
                if (
                    b.scope == "person"
                    and b.person_scope
                    and b.person_scope == person_entity
                ):
                    return name
        return "personal"

    def banks_for_person(self, person_entity: str | None) -> dict[str, MemoryBank]:
        """All banks visible to this instance, with the personal bank
        swapped for the speaker's person-scoped bank when one exists.

        When a speaker has a person-scoped bank (e.g. personal-kk for
        person.kk), the default 'personal' bank is excluded from the
        returned dict so bank='all' searches and writes never cross
        person boundaries.
        """
        result = dict(self._banks)
        if person_entity:
            scoped_name = None
            for name, b in self._banks.items():
                if (
                    b.scope == "person"
                    and b.person_scope
                    and b.person_scope == person_entity
                ):
                    scoped_name = name
                    break
            if scoped_name and scoped_name in result:
                # Remove the default personal bank — the scoped one replaces it
                result.pop("personal", None)
        # Per-speaker ACL: an allow list intersects the visible set, a deny
        # list subtracts. No policy entry → full instance set (unchanged).
        policy = self.policy_for(person_entity)
        if policy:
            allow = set(policy.get("allow") or [])
            deny = set(policy.get("deny") or [])
            if allow:
                result = {n: b for n, b in result.items() if n in allow}
            if deny:
                result = {n: b for n, b in result.items() if n not in deny}
        return result

    def policy_for(self, person_entity: str | None) -> dict[str, Any] | None:
        """Resolve the ACL policy for an identity (person entity or key
        name): exact match first; 'unknown' applies ONLY to anonymous
        sessions (no identity at all). Unlisted named identities get no
        policy — full instance access."""
        if not self.person_policies:
            return None
        if person_entity:
            return self.person_policies.get(person_entity)
        return self.person_policies.get("unknown")

    def bank_allowed(self, name: str, person_entity: str | None) -> bool:
        """Whether this speaker may access the named bank at all."""
        return name in self.banks_for_person(person_entity)

    def notebook_for(self, name: str) -> str | None:
        return self.bank(name).notebook(self.notebook_ids)

    def validate_meta(self, meta: dict[str, Any]) -> list[str]:
        """Return meta keys outside the SSOT meta_schema (empty if schema
        absent or all fields known). Warn-only — drift should surface in
        logs, not break writes."""
        if not self.schema_fields:
            return []
        return sorted(k for k in meta if k not in self.schema_fields)

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "registry": self.path,
            "bank_count": len(self._banks),
            "errors": list(self.errors),
        }


_registry: MemoryBankRegistry | None = None


def get_registry() -> MemoryBankRegistry:
    """Process-wide registry, loaded once on first use."""
    global _registry
    if _registry is None:
        _registry = MemoryBankRegistry()
    return _registry


def reset_registry() -> None:
    """Drop the cached registry (tests, config reload)."""
    global _registry
    _registry = None
