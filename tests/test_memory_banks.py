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
        with self.assertRaises(RuntimeError):
            await self.runner.execute(
                "ada_remember", {"bank": "personal", "text": "x"}
            )

    async def test_mddb_update_failure_propagates(self):
        self.runner.mddb.get_document.return_value = {
            "key": "personal/gate-remote-location",
            "meta": {"status": ["active"]},
        }
        self.runner.mddb.update_document.return_value = None
        with self.assertRaises(RuntimeError):
            await self.runner.execute(
                "ada_remember",
                {"bank": "personal", "key": "personal/gate-remote-location",
                 "text": "fixed"},
            )

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
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "ada_remember",
                {"bank": "personal", "text": "x", "supersedes": "personal/none"},
            )

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
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "ada_outcome",
                {"bank": "personal", "key": "k", "outcome": "meh"},
            )
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
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "ada_outcome",
                {"bank": "personal", "key": "personal/none", "outcome": "good"},
            )

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
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_persona", {"action": "set", "knob": "tone", "value": "direct"}
            )

    async def test_persona_show_returns_defaults(self):
        self.runner._banks = self._persona_registry()
        self.runner.session_caller_name = "admin-device"
        out = await self.runner.execute(
            "ada_persona", {"action": "show"}
        )
        self.assertEqual(out["verb"], "show")
        self.assertEqual(out["knobs"]["tone"], "warm")


if __name__ == "__main__":
    unittest.main()
