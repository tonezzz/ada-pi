import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.memory_banks import MemoryBankRegistry, doc_effective_status
from backend.tool_runner import ToolRunner

REGISTRY = {
    "banks": {
        "general": {
            "title": "General",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-general",
            "notebooklm_group": "memory",
            "kinds": ["fact", "preference", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "personal": {
            "title": "Personal",
            "scope": "instance",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-personal-{instance}",
            "kinds": ["note"],
            "writable": True,
            "write_policy": "direct",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "tony-only": {
            "title": "Tony only",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-tonyonly-{instance}",
            "kinds": ["note"],
            "writable": True,
            "write_policy": "direct",
            "allowed_tools": ["ada_remember"],
            "status": "active",
        },
        "readonly": {
            "title": "Read only",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-home",
            "kinds": ["fact"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "ops-scenarios": {
            "title": "Scenario reports (ops store)",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-scenario-reports",
            "kinds": ["report"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            # report docs' meta.status is the run outcome, not lifecycle
            "status_outcome": True,
            "status": "active",
        },
        "broken": {
            "scope": "shared",
            "instances": ["tony"],
            "mddb_collection": "",
            "status": "active",
        },
    }
}


def _registry(instance="tony", spec=None, notebook_ids=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec or REGISTRY, f)
        path = f.name
    reg = MemoryBankRegistry(
        path=path,
        instance=instance,
        notebook_ids=notebook_ids if notebook_ids is not None else {"memory": "nb-1"},
    )
    Path(path).unlink()
    return reg


class RegistryTests(unittest.TestCase):
    def test_filters_by_instance(self):
        reg = _registry(instance="michael")
        names = set(reg.banks())
        self.assertIn("general", names)
        self.assertIn("personal", names)
        self.assertNotIn("tony-only", names)
        self.assertNotIn("broken", names)  # assigned only to tony

    def test_instance_expansion(self):
        reg = _registry(instance="tony")
        self.assertEqual(
            reg.bank("personal").mddb_collection, "ada-ha-bank-personal-tony"
        )

    def test_unresolvable_bank_is_loud_error(self):
        reg = _registry(instance="tony")
        self.assertTrue(any("broken" in e for e in reg.errors))
        self.assertNotIn("broken", reg.banks())

    def test_instance_scope_requires_placeholder(self):
        spec = {
            "banks": {
                "leaky": {
                    "scope": "instance",
                    "instances": ["tony", "michael"],
                    "mddb_collection": "ada-ha-bank-shared",
                },
                "single": {
                    "scope": "instance",
                    "instances": ["tony"],
                    "mddb_collection": "ada-ha-bank-single-tony",
                },
            }
        }
        reg = _registry(instance="tony", spec=spec)
        self.assertNotIn("leaky", reg.banks())
        self.assertTrue(any("leaky" in e for e in reg.errors))
        # A single-instance bank may hardcode the instance in its collection.
        self.assertIn("single", reg.banks())
        self.assertEqual(reg.bank("single").mddb_collection, "ada-ha-bank-single-tony")

    def test_unknown_bank_error_lists_available(self):
        reg = _registry(instance="tony")
        with self.assertRaises(KeyError) as ctx:
            reg.bank("nope")
        self.assertIn("general", str(ctx.exception))

    def test_notebook_resolution(self):
        reg = _registry(instance="tony")
        self.assertEqual(reg.notebook_for("general"), "nb-1")
        self.assertIsNone(reg.notebook_for("personal"))

    def test_missing_explicit_file_is_loud_error(self):
        reg = MemoryBankRegistry(path="/nonexistent/banks.json", instance="tony")
        self.assertFalse(reg.configured)
        self.assertTrue(reg.errors)

    def test_missing_default_file_disables_quietly(self):
        from unittest.mock import patch
        import backend.memory_banks as mb
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(mb, "DEFAULT_REGISTRY_PATH", "/nonexistent/banks.json"):
            reg = MemoryBankRegistry(instance="tony")
        self.assertFalse(reg.configured)
        self.assertEqual(reg.errors, [])

    def _acl_spec(self):
        spec = json.loads(json.dumps(REGISTRY))
        spec["banks"]["personal-testo"] = {
            "scope": "person",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-personal-testo",
            "writable": True,
            "allowed_tools": ["ada_remember"],
            "person_scope": "person.testo_2",
            "status": "active",
        }
        spec["person_policies"] = {
            "person.testo_2": {"allow": ["general", "readonly", "personal-testo"]},
            "testo": {"allow": ["general", "readonly", "personal-testo"]},
            "unknown": {"allow": ["general"]},
        }
        return spec

    def test_person_policy_allow_list(self):
        reg = _registry(instance="tony", spec=self._acl_spec())
        names = set(reg.banks_for_person("person.testo_2"))
        self.assertEqual(names, {"general", "readonly", "personal-testo"})

    def test_key_name_policy(self):
        reg = _registry(instance="tony", spec=self._acl_spec())
        names = set(reg.banks_for_person("testo"))
        self.assertEqual(names, {"general", "readonly", "personal-testo"})

    def test_unknown_policy_for_anonymous(self):
        reg = _registry(instance="tony", spec=self._acl_spec())
        names = set(reg.banks_for_person(None))
        self.assertEqual(names, {"general"})

    def test_unlisted_identity_gets_full_set(self):
        reg = _registry(instance="tony", spec=self._acl_spec())
        names = set(reg.banks_for_person("person.tony"))
        self.assertIn("personal", names)
        self.assertIn("tony-only", names)

    def test_bank_allowed_helper(self):
        reg = _registry(instance="tony", spec=self._acl_spec())
        self.assertFalse(reg.bank_allowed("personal", "testo"))
        self.assertTrue(reg.bank_allowed("general", "testo"))
        self.assertTrue(reg.bank_allowed("personal", "person.tony"))

    def test_deny_list_subtracts(self):
        spec = self._acl_spec()
        spec["person_policies"]["testo"] = {"deny": ["general"]}
        reg = _registry(instance="tony", spec=spec)
        names = set(reg.banks_for_person("testo"))
        self.assertNotIn("general", names)
        self.assertIn("personal", names)

    def _persona_spec(self):
        spec = self._acl_spec()
        spec["persona"] = {
            "doc_key": "persona/self",
            "knobs": {
                "tone": {"values": ["warm", "direct"], "default": "warm"},
                "verbosity": {"values": ["brief", "normal", "detailed"], "default": "normal"},
                "address_name": {"type": "string", "max": 40, "default": ""},
                "emoji": {"type": "bool", "default": False},
            },
        }
        return spec

    def test_persona_bank_resolution_by_identity(self):
        reg = _registry(instance="tony", spec=self._persona_spec())
        from backend.memory_ops import persona_bank_for
        self.assertEqual(persona_bank_for(reg, "person.tony").name, "personal")
        self.assertEqual(persona_bank_for(reg, "testo").name, "personal-testo")
        self.assertIsNone(persona_bank_for(reg, None))  # anon: general isn't personal

    def test_persona_knob_validation(self):
        reg = _registry(instance="tony", spec=self._persona_spec())
        from backend.memory_ops import _validate_persona_knob
        self.assertEqual(_validate_persona_knob(reg, "tone", "direct"), "direct")
        self.assertTrue(_validate_persona_knob(reg, "emoji", "yes"))
        with self.assertRaises(ValueError):
            _validate_persona_knob(reg, "tone", "angry")
        with self.assertRaises(ValueError):
            _validate_persona_knob(reg, "nonsense", "x")

    def test_persona_instruction_only_custom(self):
        reg = _registry(instance="tony", spec=self._persona_spec())
        from backend.memory_ops import persona_instruction
        self.assertIsNone(persona_instruction(
            {"tone": "warm", "verbosity": "normal", "address_name": "", "emoji": False}, reg))
        line = persona_instruction(
            {"tone": "direct", "verbosity": "normal", "address_name": "T", "emoji": False}, reg)
        self.assertIn("tone=direct", line)
        self.assertIn("address_name=T", line)
        self.assertNotIn("verbosity", line)

    def test_default_policy_for_unlisted_identity(self):
        spec = self._acl_spec()
        spec["person_policies"]["default"] = {"allow": ["general", "home2"]}
        spec["banks"]["home2"] = dict(spec["banks"]["readonly"])
        reg = _registry(instance="tony", spec=spec)
        names = set(reg.banks_for_person("brand-new-person"))
        self.assertEqual(names, {"general", "home2"})

    def test_full_policy_bypasses_acl(self):
        spec = self._acl_spec()
        spec["person_policies"]["person.tony"] = {"full": True}
        reg = _registry(instance="tony", spec=spec)
        names = set(reg.banks_for_person("person.tony"))
        self.assertIn("personal", names)
        self.assertIn("tony-only", names)

    def test_control_policy_allow_domains(self):
        spec = self._acl_spec()
        spec["control_policies"] = {
            "testo": {"allow_domains": ["light", "switch"]},
            "person.tony": {"full": True},
            "unknown": {"allow_domains": ["light"]},
        }
        reg = _registry(instance="tony", spec=spec)
        self.assertTrue(reg.control_allowed("light.kitchen", "testo"))
        self.assertFalse(reg.control_allowed("cover.gate", "testo"))
        self.assertFalse(reg.control_allowed("lock.front", "testo"))
        self.assertTrue(reg.control_allowed("cover.gate", "person.tony"))
        self.assertFalse(reg.control_allowed("switch.tv", None))
        self.assertTrue(reg.control_allowed("light.hall", None))

    def test_control_policy_deny_overrides(self):
        spec = self._acl_spec()
        spec["control_policies"] = {
            "testo": {"allow_domains": ["switch"], "deny_entities": ["switch.plug_tv"]},
        }
        reg = _registry(instance="tony", spec=spec)
        self.assertFalse(reg.control_allowed("switch.plug_tv", "testo"))
        self.assertTrue(reg.control_allowed("switch.fan", "testo"))

    def test_effective_status_lazy_expiry(self):
        doc = {"meta": {"status": ["active"], "valid_until": ["2020-01-01"]}}
        self.assertEqual(doc_effective_status(doc, today="2026-01-01"), "expired")
        doc["meta"]["valid_until"] = ["2099-01-01"]
        self.assertEqual(doc_effective_status(doc, today="2026-01-01"), "active")
        doc["meta"]["status"] = ["retracted"]
        self.assertEqual(doc_effective_status(doc), "retracted")


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _registry(instance="tony")
        self.runner.mddb = AsyncMock()
        # non-async helper on the real client; AsyncMock would auto-mock
        # it truthy and skip vector_search as if ops-routed
        self.runner.mddb.is_ops_routed = lambda c: False
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.get_document.return_value = None
        self.runner.mddb.add_document.return_value = {}
        self.runner.mddb.update_document.return_value = {}

    async def test_confirmed_policy_requires_confirmation(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_remember", {"bank": "general", "text": "x"}
            )
        self.runner.mddb.add_document.assert_not_called()

    async def test_confirmed_write_with_confirmation(self):
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "general", "text": "likes espresso", "confirmed": True},
        )
        self.assertEqual(out["verb"], "create")
        self.runner.mddb.add_document.assert_called_once()
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-general")
        self.assertEqual(args[4]["scope"], ["shared"])

    async def test_direct_policy_writes_without_confirmation(self):
        out = await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "private note"}
        )
        self.assertEqual(out["verb"], "create")
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-tony")
        self.assertEqual(args[4]["scope"], ["test"])

    async def test_sensitive_write_rerouted_to_personal(self):
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "general", "confirmed": True,
             "text": "Mr Mano's passport and ID card copies are in A-68"},
            identity="person.kk",
        )
        self.assertEqual(out["verb"], "create")
        self.assertEqual(out["bank"], "personal")
        self.assertEqual(out["rerouted_from"], "general")
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-tony")

    async def test_nonsensitive_shared_write_not_rerouted(self):
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "general", "confirmed": True,
             "text": "the hallway light bulb is 60W"},
            identity="person.kk",
        )
        self.assertEqual(out["bank"], "general")
        self.assertNotIn("rerouted_from", out)

    async def test_mddb_write_failure_propagates(self):
        self.runner.mddb.add_document.return_value = None
        out = await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "x"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "RuntimeError")

    async def test_mddb_update_failure_propagates(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/gate-remote-location",
            "meta": {"status": ["active"]},
        }
        self.runner.mddb.update_document.return_value = None
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal", "key": "personal/gate-remote-location",
             "text": "fixed"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "RuntimeError")

    async def test_readonly_bank_denied(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_remember", {"bank": "readonly", "text": "x"}
            )

    async def test_tool_not_in_allowed_tools_denied(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_forget", {"bank": "tony-only", "key": "k"}
            )

    async def test_unknown_bank_is_value_error(self):
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "ada_remember", {"bank": "nope", "text": "x"}
            )

    async def test_correct_in_place_via_subject(self):
        self.runner.mddb.search_documents.return_value = [
            {
                "key": "personal/gate-remote-location",
                "meta": {
                    "status": ["active"],
                    "subject": ["gate-remote"],
                    "attribute": ["location"],
                    "valid_from": ["2026-01-01"],
                    "kind": ["fact"],
                },
            }
        ]
        out = await self.runner.execute(
            "ada_remember",
            {
                "bank": "personal",
                "text": "gate remote moved to the cabinet",
                "subject": "gate-remote",
                "attribute": "location",
            },
        )
        self.assertEqual(out["verb"], "correct")
        self.assertEqual(out["key"], "personal/gate-remote-location")
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["valid_from"], ["2026-01-01"])  # preserved
        self.assertEqual(kw["meta"]["kind"], ["fact"])  # preserved
        self.assertEqual(kw["meta"]["status"], ["active"])

    async def test_supersede_marks_old_doc(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/old",
            "meta": {"status": ["active"], "subject": ["s"]},
        }
        out = await self.runner.execute(
            "ada_remember",
            {
                "bank": "personal",
                "text": "new fact",
                "key": "personal/new",
                "supersedes": "personal/old",
            },
        )
        self.assertEqual(out["verb"], "supersede")
        add_args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(add_args[4]["supersedes"], ["personal/old"])
        upd_args = self.runner.mddb.update_document.call_args.args
        upd_kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(upd_args[1], "personal/old")
        self.assertEqual(upd_kw["meta"]["status"], ["superseded"])
        self.assertEqual(upd_kw["meta"]["superseded_by"], ["personal/new"])

    async def test_supersede_missing_doc_fails(self):
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal", "text": "x", "supersedes": "personal/none"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_forget_retracts(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/x",
            "meta": {"status": ["active"], "kind": ["note"]},
        }
        out = await self.runner.execute(
            "ada_forget",
            {"bank": "personal", "key": "personal/x", "reason": "wrong"},
        )
        self.assertEqual(out["verb"], "retract")
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["status"], ["retracted"])
        self.assertEqual(kw["meta"]["retracted_reason"], ["wrong"])
        self.assertEqual(kw["meta"]["kind"], ["note"])  # preserved

    def _person_scope_registry(self):
        spec = json.loads(json.dumps(REGISTRY))
        spec["banks"]["personal-testo"] = {
            "scope": "person",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-personal-testo",
            "kinds": ["note"],
            "writable": True,
            "write_policy": "direct",
            "allowed_tools": ["ada_remember", "ada_forget"],
            "person_scope": "person.testo_2",
            "key_scope": ["testo", "user-testo"],
            "status": "active",
        }
        return _registry(instance="tony", spec=spec)

    async def test_person_scope_write_denied_for_other_identity(self):
        self.runner._banks = self._person_scope_registry()
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal-testo", "text": "not his note"},
            identity="person.tony",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")
        self.assertIn("private to its owner", out["error"])
        self.runner.mddb.add_document.assert_not_called()

    async def test_person_scope_write_denied_for_anonymous(self):
        self.runner._banks = self._person_scope_registry()
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal-testo", "text": "anon note"},
            identity=None,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")
        self.runner.mddb.add_document.assert_not_called()

    async def test_person_scope_write_allowed_for_owner(self):
        self.runner._banks = self._person_scope_registry()
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal-testo", "text": "his own note"},
            identity="person.testo_2",
        )
        self.assertEqual(out["verb"], "create")
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-testo")

    async def test_person_scope_write_allowed_for_key_scope(self):
        self.runner._banks = self._person_scope_registry()
        out = await self.runner.execute(
            "ada_remember",
            {"bank": "personal-testo", "text": "his own note"},
            identity="testo",
        )
        self.assertEqual(out["verb"], "create")

    async def test_person_scope_forget_denied_for_other_identity(self):
        self.runner._banks = self._person_scope_registry()
        out = await self.runner.execute(
            "ada_forget",
            {"bank": "personal-testo", "key": "personal-testo/x"},
            identity="person.kk",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")
        self.runner.mddb.update_document.assert_not_called()

    async def test_ops_routed_bank_skips_vector_and_windows_listing(self):
        # The ops store has no embeddings: vector_search is a guaranteed
        # 400 and listings are oldest-first, so the degraded path must
        # bound candidates to recent last_verified days (2026-10-04:
        # scenario_report_recall could not see fresh reports).
        self.runner.mddb.is_ops_routed = lambda c: True
        self.runner.mddb.search_documents.return_value = [
            {"key": "report/doc-recall-live-20261004-1200",
             "contentMd": "# doc-recall-live — pass", "meta": {
                 # run outcome — must not be lifecycle-filtered
                 "status": ["fail"],
                 "last_verified": ["2026-10-04"]}},
        ]
        out = await self.runner.execute(
            "ada_memory_search",
            {"bank": "ops-scenarios", "query": "doc-recall pass"},
        )
        self.runner.mddb.vector_search.assert_not_called()
        fm = self.runner.mddb.search_documents.call_args.kwargs.get(
            "filter_meta") or {}
        self.assertIn("last_verified", fm)
        self.assertTrue(any("doc-recall" in h["key"] for h in out["hits"]))

    async def test_search_filters_expired(self):
        self.runner.mddb.vector_search.return_value = [
            {"key": "a", "contentMd": "old", "meta": {"status": ["active"], "valid_until": ["2020-01-01"]}},
            {"key": "b", "contentMd": "fresh", "meta": {"status": ["active"]}},
        ]
        out = await self.runner.execute(
            "ada_memory_search", {"bank": "general", "query": "x"}
        )
        self.assertEqual([h["key"] for h in out["hits"]], ["b"])

    async def test_remember_dedupes_high_similarity_hit(self):
        # No subject/attribute match, but a near-identical active doc exists
        # -> correct it in place instead of creating a duplicate.
        self.runner.mddb.vector_search.return_value = [
            {"key": "personal/espresso", "score": 0.91,
             "meta": {"status": ["active"], "kind": ["preference"], "valid_from": ["2026-09-01"]}},
        ]
        out = await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "I really like espresso"}
        )
        self.assertEqual(out["verb"], "correct")
        self.assertEqual(out["key"], "personal/espresso")
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["kind"], ["preference"])  # preserved

    async def test_remember_creates_when_similarity_below_threshold(self):
        self.runner.mddb.vector_search.return_value = []  # no hit >= 0.85
        out = await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "something new entirely"}
        )
        self.assertEqual(out["verb"], "create")
        self.runner.mddb.add_document.assert_called_once()

    async def test_remember_stamps_session_id(self):
        self.runner.session_id = "sess-42"
        self.runner.mddb.vector_search.return_value = []
        await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "something new"}
        )
        meta = self.runner.mddb.add_document.call_args.args[4]
        self.assertEqual(meta["session_id"], ["sess-42"])

    async def test_remember_omits_unknown_session(self):
        self.runner.session_id = "unknown"
        self.runner.mddb.vector_search.return_value = []
        await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "something new"}
        )
        meta = self.runner.mddb.add_document.call_args.args[4]
        self.assertNotIn("session_id", meta)

    async def test_forget_stamps_retracted_by_session(self):
        self.runner.session_id = "sess-9"
        self.runner.mddb.get_document.return_value = {
            "key": "personal/x",
            "meta": {"status": ["active"], "kind": ["note"]},
        }
        await self.runner.execute(
            "ada_forget", {"bank": "personal", "key": "personal/x"}
        )
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["retracted_by_session"], ["sess-9"])

    async def test_search_falls_back_when_vector_fails(self):
        self.runner.mddb.vector_search.return_value = None
        self.runner.mddb.search_documents.return_value = [
            {"key": "b", "contentMd": "fresh", "meta": {"status": ["active"]}},
        ]
        out = await self.runner.execute(
            "ada_memory_search", {"bank": "general", "query": "x"}
        )
        self.assertTrue(out["degraded"])
        self.assertEqual([h["key"] for h in out["hits"]], ["b"])

    async def test_search_marks_low_confidence_unverified(self):
        self.runner.mddb.vector_search.return_value = [
            {"key": "a", "contentMd": "shaky fact",
             "meta": {"status": ["active"], "confidence": ["0.2"]}},
            {"key": "b", "contentMd": "solid fact",
             "meta": {"status": ["active"], "confidence": ["0.9"]}},
            {"key": "c", "contentMd": "no confidence",
             "meta": {"status": ["active"]}},
        ]
        out = await self.runner.execute(
            "ada_memory_search", {"bank": "general", "query": "x"}
        )
        hits = {h["key"]: h for h in out["hits"]}
        self.assertTrue(hits["a"]["unverified"])
        self.assertNotIn("unverified", hits["b"])
        self.assertNotIn("confidence", hits["c"])

    async def test_search_records_use(self):
        self.runner.mddb.vector_search.return_value = [
            {"key": "a", "contentMd": "fact",
             "meta": {"status": ["active"], "use_count": ["3"]}},
        ]
        await self.runner.execute(
            "ada_memory_search", {"bank": "general", "query": "x"}
        )
        await asyncio.sleep(0)  # let the fire-and-forget task run
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["use_count"], ["4"])
        self.assertIn("last_used", kw["meta"])

    async def test_search_skips_use_count_for_listing(self):
        self.runner.mddb.search_documents.return_value = [
            {"key": "a", "contentMd": "fact", "meta": {"status": ["active"]}},
        ]
        await self.runner.execute(
            "ada_memory_search", {"bank": "general", "query": "*"}
        )
        await asyncio.sleep(0)
        self.runner.mddb.update_document.assert_not_called()

    async def test_outcome_good_bumps_confidence_and_verified(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/x",
            "meta": {"status": ["active"], "confidence": ["0.7"],
                     "last_verified": ["2020-01-01"]},
        }
        out = await self.runner.execute(
            "ada_outcome",
            {"bank": "personal", "key": "personal/x", "outcome": "worked"},
        )
        self.assertEqual(out["verb"], "outcome")
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["outcome"], ["worked"])
        self.assertEqual(kw["meta"]["confidence"], ["0.8"])
        self.assertNotEqual(kw["meta"]["last_verified"], ["2020-01-01"])

    async def test_outcome_bad_lowers_confidence(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/x",
            "meta": {"status": ["active"], "confidence": ["0.5"]},
        }
        await self.runner.execute(
            "ada_outcome",
            {"bank": "personal", "key": "personal/x", "outcome": "bad"},
        )
        kw = self.runner.mddb.update_document.call_args.kwargs
        self.assertEqual(kw["meta"]["confidence"], ["0.3"])

    async def test_outcome_rejects_unknown_value(self):
        out = await self.runner.execute(
            "ada_outcome",
            {"bank": "personal", "key": "k", "outcome": "meh"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        self.runner.mddb.update_document.assert_not_called()

    async def test_outcome_confirmed_policy_needs_confirmation(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_outcome",
                {"bank": "general", "key": "k", "outcome": "good"},
            )
        self.runner.mddb.update_document.assert_not_called()

    async def test_outcome_missing_doc_fails(self):
        self.runner.mddb.get_document.return_value = None
        out = await self.runner.execute(
            "ada_outcome",
            {"bank": "personal", "key": "personal/none", "outcome": "good"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    def _persona_registry(self):
        spec = {
            "banks": {
                "general": {
                    "scope": "shared", "instances": ["tony"],
                    "mddb_collection": "ada-ha-bank-general",
                    "writable": True, "status": "active",
                },
                "personal": {
                    "scope": "instance", "instances": ["tony"],
                    "mddb_collection": "ada-ha-bank-personal-{instance}",
                    "writable": True, "status": "active",
                },
                "personal-testo": {
                    "scope": "person", "instances": ["tony"],
                    "mddb_collection": "ada-ha-bank-personal-testo",
                    "writable": True, "status": "active",
                    "person_scope": "person.testo_2",
                },
            },
            "person_policies": {
                "testo": {"allow": ["general", "personal-testo"]},
                "unknown": {"allow": ["general"]},
            },
            "persona": {
                "doc_key": "persona/self",
                "knobs": {
                    "tone": {"values": ["warm", "direct"], "default": "warm"},
                    "verbosity": {"values": ["brief", "normal"], "default": "normal"},
                },
            },
        }
        return _registry(instance="tony", spec=spec)

    async def test_persona_set_writes_to_personal_bank(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin-device"
        out = await self.runner.execute(
            "ada_persona", {"action": "set", "knob": "tone", "value": "direct"}
        )
        self.assertEqual(out["verb"], "create")
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-tony")
        self.assertEqual(args[1], "persona/self")
        self.assertIn("tone: direct", args[3])
        self.assertIn("apply", out)

    async def test_persona_set_routes_to_scoped_bank_for_testo(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "testo"
        await self.runner.execute(
            "ada_persona", {"action": "set", "knob": "verbosity", "value": "brief"}
        )
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-testo")

    async def test_persona_set_denied_for_anonymous(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = None
        self.runner.current_speaker_ha_person = None
        out = await self.runner.execute(
            "ada_persona", {"action": "set", "knob": "tone", "value": "direct"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")

    async def test_persona_show_returns_defaults(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin-device"
        out = await self.runner.execute(
            "ada_persona", {"action": "show"}
        )
        self.assertEqual(out["verb"], "show")
        self.assertEqual(out["knobs"]["tone"], "warm")

    def _mock_persons(self, people):
        self.runner.context.ha_client.persons = AsyncMock(return_value=people)
        self.runner.context.ha_client.resolve_person = AsyncMock(
            side_effect=lambda q: next(
                (p for p in people
                 if p["entity_id"] == q or p["name"].lower() == q.lower()
                 or p["entity_id"] == f"person.{q.lower()}"),
                None,
            )
        )

    async def test_persona_list_shows_ha_people(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin-device"
        self._mock_persons([
            {"entity_id": "person.kk", "name": "KK", "state": "home"},
            {"entity_id": "person.testo_2", "name": "Testo", "state": "not_home"},
        ])
        out = await self.runner.execute("ada_persona", {"action": "list"})
        self.assertEqual(out["verb"], "list")
        self.assertEqual(
            [p["entity_id"] for p in out["persons"]],
            ["person.kk", "person.testo_2"],
        )
        # testo_2's person-scoped bank claims it; person.kk is unlisted so it
        # falls back to the default 'personal' bank (no restrictive policy).
        self.assertEqual(out["persons"][0]["persona_bank"], "personal")
        self.assertEqual(out["persons"][1]["persona_bank"], "personal-testo")

    async def test_persona_set_other_person_as_admin(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin"
        self._mock_persons([
            {"entity_id": "person.testo_2", "name": "Testo", "state": "home"},
        ])
        out = await self.runner.execute(
            "ada_persona",
            {"action": "set", "person": "Testo", "knob": "tone", "value": "direct"},
        )
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-testo")
        self.assertEqual(out["person"], "person.testo_2")
        self.assertIn("their sessions", out["apply"])

    async def test_persona_set_other_person_denied_for_restricted(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "testo"
        self._mock_persons([
            {"entity_id": "person.tony", "name": "Tony", "state": "home"},
        ])
        out = await self.runner.execute(
            "ada_persona",
            {"action": "set", "person": "Tony", "knob": "tone", "value": "direct"},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")
        self.runner.mddb.add_document.assert_not_called()

    async def test_persona_key_bound_ha_person_routes_identity(self):
        # A key issued with ha_person=person.testo_2 makes the session
        # identity person.testo_2 even without a voiceprint — persona
        # writes land in the person-scoped bank.
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "user-testo"
        self.runner.session_caller_ha_person = "person.testo_2"
        self.assertEqual(self.runner._memory_identity(), "person.testo_2")
        await self.runner.execute(
            "ada_persona", {"action": "set", "knob": "tone", "value": "direct"}
        )
        args = self.runner.mddb.add_document.call_args.args
        self.assertEqual(args[0], "ada-ha-bank-personal-testo")

    async def test_persona_set_self_by_name_via_same_bank(self):
        # Key 'testo' resolves to the same persona bank as person.testo_2 —
        # naming yourself is self-targeting, not a cross-person write.
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "testo"
        self._mock_persons([
            {"entity_id": "person.testo_2", "name": "Testo", "state": "home"},
        ])
        out = await self.runner.execute(
            "ada_persona",
            {"action": "set", "person": "person.testo_2", "knob": "tone",
             "value": "direct"},
        )
        self.assertEqual(out["person"], "person.testo_2")

    async def test_persona_unknown_person_lists_known(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin"
        self._mock_persons([
            {"entity_id": "person.kk", "name": "KK", "state": "home"},
        ])
        out = await self.runner.execute(
            "ada_persona", {"action": "show", "person": "nobody"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        self.assertIn("person.kk", out["error"])

    def _pin_session(self, caller, person, owner=None, speaker=None):
        self.runner.session_caller_name = caller
        self.runner.session_caller_ha_person = person
        self.runner.session_owner_identity = (
            owner if owner is not None else (person or caller))
        self.runner.current_speaker_ha_person = speaker

    async def test_policy_identity_prefers_owner_over_speaker(self):
        # Session-security P1: the pinned owner authorizes, never the
        # currently-speaking voice.
        self._pin_session("user-kk", "person.kk", speaker="person.tony")
        self.assertEqual(self.runner.policy_identity(), "person.kk")
        self.assertEqual(self.runner._memory_identity(), "person.tony")

    async def test_secondary_speaker_denied_writes(self):
        self.runner._banks = _registry(instance="tony")
        self._pin_session("user-kk", "person.kk", speaker="person.tony")
        self.assertTrue(self.runner._is_secondary_turn())
        for tool, args in (
            ("ada_remember", {"bank": "personal", "text": "x"}),
            ("control_entity", {"entity_id": "light.den", "on": True}),
            ("ada_doc_search", {"query": "id"}),
            ("ada_memory_search", {"bank": "general", "query": "x"}),
            ("ada_enroll_speaker", {"name": "Tony"}),
        ):
            # Gate denials still raise; ada_enroll_speaker's own check
            # lives inside the method — the boundary normalizes it to
            # the canonical {ok: False, error_type: "PermissionError"}.
            if tool == "ada_enroll_speaker":
                out = await self.runner.execute(tool, args)
                self.assertFalse(out["ok"], tool)
                self.assertEqual(out["error_type"], "PermissionError")
                continue
            with self.assertRaises(PermissionError, msg=tool):
                await self.runner.execute(tool, args)
        self.runner.mddb.add_document.assert_not_called()

    async def test_secondary_speaker_denied_persona_write(self):
        self.runner._banks = self._persona_registry()
        self._pin_session("user-kk", "person.kk", speaker="person.tony")
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_persona",
                {"action": "set", "knob": "tone", "value": "direct"})
        # Reads still work — a guest may show (their own) persona.
        out = await self.runner.execute(
            "ada_persona", {"action": "show"})
        self.assertEqual(out["verb"], "show")
        self.assertEqual(out["person"], "person.tony")

    async def test_owner_voice_restores_rights(self):
        self.runner._banks = _registry(instance="tony")
        self.runner.mddb.vector_search.return_value = []
        self._pin_session("user-kk", "person.kk", speaker="person.kk")
        self.assertFalse(self.runner._is_secondary_turn())
        out = await self.runner.execute(
            "ada_remember", {"bank": "personal", "text": "owner fact"})
        self.assertEqual(out["verb"], "create")

    async def test_key_name_alias_not_secondary(self):
        # Unbound key 'tony' + speaker person.tony must not self-lock the
        # owner — the person.<slug> alias keeps them the same identity.
        self._pin_session("tony", None, owner="tony", speaker="person.tony")
        self.assertFalse(self.runner._is_secondary_turn())

    async def test_unrecognized_voice_not_secondary(self):
        # No positive non-owner identification -> not a secondary turn
        # (unenrolled owners must not lock themselves out; policy P6 edge).
        self._pin_session("user-kk", "person.kk", speaker=None)
        self.assertFalse(self.runner._is_secondary_turn())

    async def test_stale_speaker_label_not_secondary(self):
        # 2026-10-01: one confident KK hit stayed pinned while Tony's
        # far-field chunks failed the match threshold — every cast was
        # denied under a label that had stopped re-confirming. A label
        # older than SPEAKER_STALE_S is treated as unrecognized.
        from backend import tool_runner as tr_mod
        self._pin_session("admin", None, owner="admin",
                          speaker="person.kk")
        token = tr_mod._CALLER_SPEAKER_SESSION.set(
            type("SS", (), {"speaker_age_s": lambda self: 999.0})())
        try:
            self.assertFalse(self.runner._is_secondary_turn())
        finally:
            tr_mod._CALLER_SPEAKER_SESSION.reset(token)

    async def test_fresh_speaker_label_still_secondary(self):
        # A label that keeps re-confirming stays enforced — the stale
        # exemption only applies past SPEAKER_STALE_S.
        from backend import tool_runner as tr_mod
        self._pin_session("admin", None, owner="admin",
                          speaker="person.kk")
        token = tr_mod._CALLER_SPEAKER_SESSION.set(
            type("SS", (), {"speaker_age_s": lambda self: 5.0})())
        try:
            self.assertTrue(self.runner._is_secondary_turn())
        finally:
            tr_mod._CALLER_SPEAKER_SESSION.reset(token)

    async def test_persona_cross_target_fails_closed_without_scoped_bank(self):
        # Admin targets a person with no scoped bank — refuse rather than
        # file their persona under the default 'personal' bank.
        self.runner._banks = self._persona_registry()
        self._pin_session("admin", None, owner="admin")
        self._mock_persons([
            {"entity_id": "person.bob", "name": "Bob", "state": "home"},
        ])
        out = await self.runner.execute(
            "ada_persona",
            {"action": "set", "person": "Bob", "knob": "tone",
             "value": "direct"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")
        self.assertIn("person-scoped", out["error"])
        self.runner.mddb.add_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
