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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.instance import ada_instance_id

logger = logging.getLogger("memory.banks")

DEFAULT_REGISTRY_PATH = os.path.expanduser("~/.config/ada/memory-banks.json")

# Runtime device-ACL grants — owner tools write here; control_allowed()
# merges them on top of the registry's control_policies without a
# registry re-render or service restart. Shape:
#   {"person.kk": {"light.bedroom": {"granted_at": iso, "by": "person.tony"}}}
GRANTS_PATH = Path(os.environ.get(
    "ADA_DEVICE_GRANTS_FILE",
    str(Path.home() / ".config" / "ada" / "device-grants.json")))
_grants_cache: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})


def device_grants() -> dict[str, dict[str, dict[str, Any]]]:
    """{identity: {entity_id: {...}}} — reloaded on file mtime change."""
    global _grants_cache
    try:
        mtime = GRANTS_PATH.stat().st_mtime
    except OSError:
        _grants_cache = (0.0, {})
        return {}
    if mtime != _grants_cache[0]:
        try:
            data = json.loads(GRANTS_PATH.read_text())
            _grants_cache = (mtime, data if isinstance(data, dict) else {})
        except Exception as exc:
            logger.warning("device-grants read failed: %s", exc)
            _grants_cache = (mtime, {})
    return _grants_cache[1]
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
    key_scope: list[str] = field(default_factory=list)  # issued-key names that also route to this person bank
    prompt_hidden: bool = False  # excluded from {writable_banks} in tool schemas — callable but never suggested
    # True when doc meta.status holds a *run outcome* (pass/fail/flaky)
    # rather than a memory lifecycle — recall must not gate on it.
    # (ops-scenarios reports: every doc was dropped as non-'active',
    # 2026-10-05.)
    status_outcome: bool = False

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
        self.control_policies: dict[str, dict[str, Any]] = {}
        self.session_security: dict[str, Any] = {}
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
        self.control_policies = (
            data.get("control_policies") if isinstance(data, dict) else None
        ) or {}
        self.session_security = (
            data.get("session_security") if isinstance(data, dict) else None
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
            key_scope=list(spec.get("key_scope") or []),
            prompt_hidden=bool(spec.get("prompt_hidden")),
            status_outcome=bool(spec.get("status_outcome")),
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

    def _scoped_bank_name(self, identity: str | None) -> str | None:
        """Person-scoped bank for this identity: person_scope match first,
        then key_scope (issued-key names that route to the same bank)."""
        if not identity:
            return None
        for name, b in self._banks.items():
            if b.scope == "person" and (
                b.person_scope == identity or identity in b.key_scope
            ):
                return name
        return None

    def personal_bank_name(self, person_entity: str | None) -> str:
        """Resolve the effective personal bank name for a speaker.

        If a person-scoped bank claims this identity (person_scope, or
        key_scope for unenrolled speakers using their own key), return
        that bank's name. Otherwise return the default 'personal' bank.
        """
        return self._scoped_bank_name(person_entity) or "personal"

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
            scoped_name = self._scoped_bank_name(person_entity)
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
            # Restricted-by-default: unlisted named identities fall back to
            # the 'default' policy when one is declared (else full access).
            return self.person_policies.get(person_entity) or self.person_policies.get("default")
        return self.person_policies.get("unknown")

    def bank_allowed(self, name: str, person_entity: str | None) -> bool:
        """Whether this speaker may access the named bank at all."""
        return bool(self.acl_decision(person_entity, bank=name)["allowed"])

    # ---------- unified ACL resolver (card ada-acl-explain) ----------
    #
    # acl_decision() is the ONE function every allow/deny answer flows
    # through: the actuation gate (_check_control_allowed), the
    # memory-write gate (_check_memory_write_allowed), the bank-read
    # ACL (memory_ops._check_bank_allowed via bank_allowed), and the
    # explain path (ada_ops action='acl_explain'). It walks the policy
    # layers in evaluation order and records each step in `trace`, so
    # "why can kk control the TV" names the deciding layer in one call
    # instead of a three-file investigation. Layer names:
    #   registry        — bank assigned to this instance (banks map)
    #   person_scope    — person-scoped bank replaced 'personal'
    #   person_policies — registry person_policies allow/deny/full
    #   control_policies— registry control_policies lists/full
    #   device_grants   — ~/.config/ada/device-grants.json overlay
    #   bank.writable / bank.allowed_tools / bank.write_policy —
    #                     per-bank spec gates (tool= writes only)
    #   bank.person_scope — person-scoped banks reject foreign writes

    @staticmethod
    def _policy_source(
        policies: dict[str, dict[str, Any]], person_entity: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        """(policy, via) mirroring policy_for/control_policy_for:
        exact identity entry, else 'default' (named) / 'unknown'
        (anonymous), else none."""
        if not policies:
            return None, "unconfigured"
        if person_entity:
            policy = policies.get(person_entity)
            if policy:
                return policy, "identity"
            policy = policies.get("default")
            return (policy, "default") if policy else (None, "unlisted")
        policy = policies.get("unknown")
        return (policy, "unknown") if policy else (None, "unlisted")

    def acl_decision(
        self,
        identity: str | None,
        *,
        entity_id: str | None = None,
        bank: str | None = None,
        tool: str | None = None,
        tool_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve whether `identity` may act on the given subject and
        name the layer that decided. Subjects: entity_id -> actuation
        (control_policies + device-grants overlay); bank -> bank access
        (person_policies), with tool= adding the write-side layers
        (writable, allowed_tools, write_policy, person-scope).
        tool_aliases maps retired tool names to canonical ones for the
        allowed_tools comparison (the runner passes its _ALIASES)."""
        if entity_id:
            return self._acl_control(str(entity_id), identity)
        if bank:
            return self._acl_bank(
                str(bank), identity, tool=tool,
                tool_aliases=tool_aliases or {})
        return {
            "ok": False, "identity": identity, "allowed": False,
            "decided_by": "input", "verdict": "denied",
            "reason": "no subject — pass entity_id= and/or bank=",
            "trace": [],
        }

    def _acl_control(
        self, entity_id: str, identity: str | None
    ) -> dict[str, Any]:
        """Actuation walk — same rule order as the old control_allowed:
        full bypass -> deny_entities -> deny_domains -> device grants ->
        allow_entities -> allow_domains -> default allow."""
        trace: list[dict[str, Any]] = []

        def done(allowed: bool, layer: str, rule: str,
                 reason: str) -> dict[str, Any]:
            trace.append({"layer": layer, "rule": rule,
                          "result": "allow" if allowed else "deny",
                          "detail": reason})
            return {
                "ok": True, "subject": "control", "identity": identity,
                "entity_id": entity_id, "allowed": allowed,
                "decided_by": layer, "verdict": (
                    "allowed" if allowed else "denied"),
                "reason": reason, "trace": trace,
            }

        policy, via = self._policy_source(self.control_policies, identity)
        domain = entity_id.split(".", 1)[0]
        trace.append({
            "layer": "control_policies", "rule": "policy_lookup",
            "result": "info",
            "detail": {
                "unconfigured": "no control_policies declared",
                "identity": f"policy for {identity!r}",
                "default": "no entry for identity — 'default' applies",
                "unknown": "anonymous identity — 'unknown' applies",
                "unlisted": ("no policy matches this identity"
                             + ("" if identity else " (anonymous)")),
            }[via],
        })
        if policy is None:
            return done(True, "control_policies",
                        f"policy_lookup:{via}",
                        "no control policy applies — actuation allowed")
        if policy.get("full"):
            return done(True, "control_policies", "full",
                        "policy declares full: true — all entities allowed")
        deny_entities = set(policy.get("deny_entities") or [])
        if entity_id in deny_entities:
            return done(False, "control_policies", "deny_entities",
                        f"{entity_id} is in deny_entities")
        deny_domains = set(policy.get("deny_domains") or [])
        if domain in deny_domains:
            return done(False, "control_policies", "deny_domains",
                        f"domain '{domain}' is in deny_domains")
        grants = device_grants().get(identity or "") or {}
        if entity_id in grants:
            rec = grants[entity_id] or {}
            return done(True, "device_grants", "grant",
                        f"{entity_id} granted"
                        + (f" by {rec.get('by')}" if rec.get("by") else "")
                        + (f" at {rec.get('granted_at')}"
                           if rec.get("granted_at") else ""))
        allow_entities = set(policy.get("allow_entities") or [])
        if allow_entities and entity_id not in allow_entities:
            return done(False, "control_policies", "allow_entities",
                        f"{entity_id} not in allow_entities")
        allow_domains = set(policy.get("allow_domains") or [])
        if allow_domains and domain not in allow_domains:
            return done(False, "control_policies", "allow_domains",
                        f"domain '{domain}' not in allow_domains")
        return done(True, "control_policies", "default_allow",
                    "policy imposes no restriction on this entity")

    def _acl_bank(
        self, name: str, identity: str | None, tool: str | None = None,
        tool_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Bank-access walk — registry membership, person-scope swap,
        person_policies allow/deny, then (tool= given) the write layers:
        writable, allowed_tools, person-scope write, write_policy."""
        trace: list[dict[str, Any]] = []

        def done(allowed: bool, layer: str, rule: str,
                 reason: str, **extra: Any) -> dict[str, Any]:
            trace.append({"layer": layer, "rule": rule,
                          "result": "allow" if allowed else "deny",
                          "detail": reason})
            out = {
                "ok": True, "subject": "bank_write" if tool else "bank",
                "identity": identity, "bank": name,
                "allowed": allowed, "decided_by": layer,
                "verdict": "allowed" if allowed else "denied",
                "reason": reason, "trace": trace,
            }
            if tool:
                out["tool"] = tool
            out.update(extra)
            return out

        if name not in self._banks:
            return done(False, "registry", "unknown_bank",
                        f"unknown or unassigned memory bank {name!r}")
        bank = self._banks[name]
        scoped = self._scoped_bank_name(identity)
        if scoped and scoped in self._banks and name == "personal":
            return done(
                False, "person_scope", "scoped_swap",
                f"identity is served by person-scoped bank {scoped!r} — "
                "'personal' is not visible to it")

        policy, via = self._policy_source(self.person_policies, identity)
        trace.append({
            "layer": "person_policies", "rule": "policy_lookup",
            "result": "info",
            "detail": {
                "unconfigured": "no person_policies declared",
                "identity": f"policy for {identity!r}",
                "default": "no entry for identity — 'default' applies",
                "unknown": "anonymous identity — 'unknown' applies",
                "unlisted": ("no policy matches this identity"
                             + ("" if identity else " (anonymous)")),
            }[via],
        })
        if policy is not None:
            allow = set(policy.get("allow") or [])
            deny = set(policy.get("deny") or [])
            if allow and name not in allow:
                return done(False, "person_policies", "allow",
                            f"{name!r} is not in the policy's allow list")
            if name in deny:
                return done(False, "person_policies", "deny",
                            f"{name!r} is in the policy's deny list")
            if policy.get("full"):
                trace.append({"layer": "person_policies",
                              "rule": "full", "result": "allow",
                              "detail": "policy declares full: true"})
        if not tool:
            return done(True, "person_policies", "default_allow",
                        "policy imposes no restriction on this bank")

        if not bank.writable:
            return done(False, "bank.writable", "read_only",
                        f"memory bank '{name}' is read-only")
        # Bank configs may name an absorbed (pre-merge) tool — the runner
        # resolves entries through its alias table so the canonical tool
        # inherits the same authorization.
        aliases = tool_aliases or {}
        allowed_tools = {aliases.get(t, t) for t in bank.allowed_tools}
        if tool not in allowed_tools:
            return done(False, "bank.allowed_tools", "tool_not_listed",
                        f"tool {tool} is not allowed on memory bank "
                        f"'{name}'",
                        allowed_tools=sorted(allowed_tools))
        owners = {bank.person_scope, *bank.key_scope}
        owners.discard(None)
        if bank.scope == "person" and identity not in owners:
            return done(False, "bank.person_scope", "foreign_write",
                        f"memory bank '{name}' is private to its owner")
        out = done(True, "person_policies", "default_allow",
                   "policy imposes no restriction on this bank")
        if bank.write_policy == "confirmed":
            out["requires_confirm"] = True
            out["verdict"] = "needs_confirmation"
            out["trace"].append({
                "layer": "bank.write_policy", "rule": "confirmed",
                "result": "info",
                "detail": f"bank '{name}' write_policy=confirmed — "
                          "the call still needs confirmed=true"})
        return out

    # ---------- actuation ACL ----------

    def identity_label(self, person_entity: str | None) -> str | None:
        """Human-friendly display name for the model: policy 'label' if
        declared, else the identity string itself."""
        if not person_entity:
            return None
        for policies in (self.person_policies, self.control_policies):
            p = policies.get(person_entity)
            if p and p.get("label"):
                return str(p["label"])
        return person_entity

    def control_policy_for(self, person_entity: str | None) -> dict[str, Any] | None:
        """Same identity resolution as bank policies, over control_policies."""
        if not self.control_policies:
            return None
        if person_entity:
            return self.control_policies.get(person_entity) or self.control_policies.get("default")
        return self.control_policies.get("unknown")

    def control_allowed(self, entity_id: str, person_entity: str | None) -> bool:
        """Whether this identity may actuate entity_id under control_policies.
        {full: true} bypasses; else deny_domains/deny_entities always deny,
        an explicit admin grant (device-grants.json overlay) allows, a
        non-empty allow_entities whitelists exact entities, and a non-empty
        allow_domains whitelists domains. The global danger-pattern floor
        still applies on top — this is subtractive only. Bool view of
        acl_decision() — the layer trace lives there."""
        return bool(self.acl_decision(
            person_entity, entity_id=entity_id)["allowed"])

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


# Content markers that force a memory into the writer's personal bank —
# private documents, identity papers and named persons in a document
# context must not land in shared banks (they are visible to
# non-admin speakers like KK/guest). Shared by ada_remember (memory_ops)
# and session-end auto-extraction (conversation_memory).
SENSITIVE_MEMORY_RE = re.compile(
    r"\b(document|documents|deed|deeds|title deed|passport|id card|"
    r"identification|visa|bank account|contract|invoice|receipt|"
    r"เอกสาร|โฉนด|พาสปอร์ต|บัตรประชาชน|ทะเบียนบ้าน|หนังสือ|สัญญา)\b",
    re.IGNORECASE)


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
