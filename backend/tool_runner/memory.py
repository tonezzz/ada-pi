"""Memory-bank tools: curated banks, persona, enroll, guest.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class MemoryMixin:

    # -- Memory bank tools (curated memory; see ssot.apps.ada-memory-*.yml) --
    # Implementation lives in backend.memory_ops — these delegates keep the
    # dispatch surface (method names/signatures) stable.

    async def ada_memory_search(
        self,
        query: str,
        bank: str = "all",
        limit: int = 5,
        include_inactive: bool = False,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Search memory across scopes (tools-merge-memory):

        banks    — the curated memory banks (memory_ops.memory_search)
        sessions — the per-instance recall-summary collection (session,
                   daily, weekly rollup docs)
        guest    — the chaba guest store (replaces guest_recall)
        all      — every scope available on this instance
        """
        scope_s = str(scope or "").strip().lower()
        if not scope_s:
            # An explicit bank name narrows to that bank only — the same
            # behavior the pre-merge tool had for bank-scoped callers.
            scope_s = "all" if str(bank or "all") == "all" else "banks"
        if scope_s not in ("all", "banks", "sessions", "guest"):
            raise ValueError(
                f"unknown memory search scope {scope!r} "
                "(all|banks|sessions|guest)")
        if scope_s == "guest" and self.chaba is None:
            self._require_chaba()  # raises: guest store is chaba-only
        if scope_s == "banks" and self.mddb is None:
            raise ValueError("no curated memory banks on this instance")
        hits: list[dict[str, Any]] = []
        degraded = False
        if scope_s in ("all", "banks") and self.mddb is not None:
            res = await memory_ops.memory_search(
                self.mddb, self.banks, bank, query, limit, include_inactive,
                person_entity=self._memory_identity(),
            )
            hits.extend(res.get("hits") or [])
            degraded = bool(res.get("degraded"))
        if scope_s in ("all", "sessions") and self.mddb is not None:
            hits.extend(await self._session_hits(str(query), int(limit)))
        if scope_s in ("all", "guest") and self.chaba is not None:
            for h in self.chaba.recall(
                    str(query), session_id=self.session_id,
                    limit=int(limit)):
                hits.append({
                    "bank": "guest", "key": h.get("key"),
                    "subject": h.get("name"), "score": h.get("score"),
                    "content": h.get("text"), "at": h.get("at"),
                })
        hits.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
        hits = hits[: int(limit)]
        return {
            "bank": bank if scope_s == "banks" else scope_s,
            "scope": scope_s,
            "count": len(hits),
            "hits": hits,
            "degraded": degraded,
        }

    async def _session_hits(
        self, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """scope=sessions — search the recall-summary collection
        (session/daily/weekly/monthly summary docs) that
        ada_session_recall's mid tier reads."""
        from backend.conversation_memory import _summary_collection
        coll = _summary_collection()
        q = str(query or "").strip()
        docs = None
        if q and q != "*" and not self.mddb.is_ops_routed(coll):
            docs = await self.mddb.vector_search(
                collection=coll, query=q, limit=int(limit) * 2)
        if docs is None:
            listing_filter = None
            if self.mddb.is_ops_routed(coll):
                # Ops listings are oldest-first — bound by `date` meta so
                # recent summaries enter the candidate window.
                days = [
                    (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
                    for i in range(14)
                ]
                listing_filter = {"date": days}
            listed = await self.mddb.search_documents(
                collection=coll, filter_meta=listing_filter,
                limit=max(int(limit) * 5, 20))
            docs = (memory_ops._keyword_rank(listed, q)
                    if q and q != "*" else listed)
        hits = []
        for d in docs or []:
            meta = d.get("meta") or {}
            content = str(d.get("contentMd") or d.get("content_md") or "")
            hits.append({
                "bank": "sessions",
                "key": d.get("key"),
                "score": d.get("score"),
                "kind": _first(meta.get("kind")),
                "date": _first(meta.get("date")),
                "content": content[:800],
            })
        return hits[: int(limit)]

    async def ada_remember(
        self,
        bank: str | None = None,
        text: str | None = None,
        key: str | None = None,
        subject: str | None = None,
        attribute: str | None = None,
        kind: str | None = None,
        valid_until: str | None = None,
        applies_to: list[str] | str | None = None,
        supersedes: str | None = None,
        prime: bool | None = None,
        private: bool = False,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Store a memory. `kind` selects the store (tools-merge-memory):

        fact/preference/person/procedure/note — curated bank write
        (bank required); vocab — append the caller's own vocab log
        (absorbed vocab_note); guest — the chaba guest store, private=true
        for a promoted user's private namespace (absorbed guest_remember /
        guest_remember_private); habit — observation telemetry, delivered
        by the live session (absorbed report_habit_observation).
        """
        k = str(kind or "").strip().lower()
        if not k and self.chaba is not None:
            k = "guest"
        if k == "vocab":
            return await self._vocab_append(text, note)
        if k == "guest":
            self._require_chaba()
            if not str(text or "").strip():
                raise ValueError("remember(kind='guest') requires text")
            slug = str(key or _slug(str(text))[:40] or "note")
            if private:
                return self.chaba.remember_private(
                    self.session_id, slug, str(text))
            return self.chaba.remember(self.session_id, slug, str(text))
        if k == "habit":
            # Provider-dispatched: the observation reaches the habit
            # monitor as a ProviderEvent. A store-side call gets a clear
            # answer rather than a silent no-op.
            return {"error": (
                "habit observations are delivered by the live session "
                "monitor — this call reached the store directly")}
        if self.mddb is None or self.memory is None:
            raise PermissionError(
                "curated memory banks are not available on this instance")
        if not str(bank or "").strip():
            raise ValueError(
                "ada_remember requires 'bank' for curated-bank writes "
                "(kind fact/preference/person/procedure/note)")
        if not str(text or "").strip():
            raise ValueError("ada_remember requires 'text'")
        return await memory_ops.remember(
            self.mddb, self.banks, self.memory.instance,
            str(bank), str(text), key, subject, attribute, kind, valid_until,
            applies_to, supersedes, session_id=self.session_id,
            person_entity=self._memory_identity(), prime=prime,
        )

    async def ada_session_recall(
        self,
        question: str = "",
        scope: str | None = None,
        limit: int = 10,
        **_unused: Any,
    ) -> dict[str, Any]:
        """Conversation/home-history recall (tools-merge-memory).

        scope='history' is the absorbed ada_ha_recall — inline search of
        the stored HA memory + recorded events. scope='sessions' (the
        deep NotebookLM ask) is provider-dispatched: outside a live voice
        session there is no conversation to attach the async answer to.
        """
        scope_s = str(scope or "sessions").strip().lower()
        if scope_s == "history":
            # Models trained on the absorbed ada_ha_recall schema sometimes
            # still send query= on the canonical name too.
            q = str(question or "") or str(_unused.get("query") or "") or "*"
            return await self.ada_ha_recall(q, int(limit))
        if scope_s != "sessions":
            raise ValueError(
                f"unknown recall scope {scope!r} (sessions|history)")
        return {"error": (
            "session recall runs inside a live voice session — for a "
            "direct lookup use ada_memory_search scope='sessions' to "
            "search session summaries")}

    async def ada_persona(
        self, action: str, knob: str | None = None, value: Any = None,
        person: str | None = None, voice: str | None = None,
    ) -> dict[str, Any]:
        """Read or adjust a speaker's stored style preferences.

        Without `person` this targets the current session identity. With
        `person` (name 'KK' or entity 'person.kk') it targets that HA
        person's profile — allowed for full-access identities, or when the
        target resolves to the caller's own persona bank (e.g. an
        unenrolled speaker addressing themselves by name via key_scope).
        """
        caller = self.policy_identity()
        action = str(action or "show").lower()
        if action == "list":
            return await self._persona_list()
        # *_voice actions absorbed ada_set_voice (tools-merge-meta-voice):
        # they manage the instance's speaking voice, not a per-person
        # profile — the provider intercepts them on live sessions (it owns
        # the idle-gated reconnect); this path serves REST/alias calls.
        if action in _PERSONA_VOICE_ACTIONS:
            return self._persona_voice(action, voice)
        # "Self" follows the speaking voice (personalization); `person`
        # targeting authorizes against the session owner (P3).
        identity = self._memory_identity() or caller
        if person:
            identity = await self._resolve_persona_target(person, caller)
            if (action in ("set", "reset") and identity != caller
                    and self.banks.personal_bank_name(identity) == "personal"):
                raise PermissionError(
                    f"{identity} has no person-scoped memory bank — refusing "
                    "to file their persona under the default 'personal' bank; "
                    "provision a scoped bank (e.g. personal-<id>) first")
        if action == "show":
            p = await memory_ops.get_persona(self.mddb, self.banks, identity)
            return {"verb": "show", "person": identity, **p}
        if action == "reset":
            return await memory_ops.reset_persona(self.mddb, self.banks, identity)
        if action == "set":
            if not knob:
                raise ValueError("set requires a knob name")
            result = await memory_ops.set_persona(
                self.mddb, self.banks, identity, str(knob), value
            )
            result["person"] = identity
            # Tell the model to apply it now — the persisted doc covers
            # future sessions via the prime injection.
            if identity == caller:
                result["apply"] = (
                    f"Preference saved and active now: {knob}={value}. "
                    "Honor it in your next replies without announcing the mechanism."
                )
            else:
                result["apply"] = (
                    f"Preference saved for {identity}: {knob}={value}. "
                    "It applies to their sessions, not this speaker's."
                )
            return result
        raise ValueError(
            f"unknown persona action {action!r} "
            "(set|show|reset|list|set_voice|show_voice|list_voices)")

    def _persona_voice(self, action: str, voice: Any) -> dict[str, Any]:
        """The absorbed ada_set_voice seat on ada_persona. A live-session
        call is intercepted provider-side (the provider owns the
        idle-gated reconnect); this runner path serves REST/alias calls —
        a set persists the choice for the next session."""
        from backend import voice_config
        if action == "set_voice" and voice:
            name = voice_config.canonical_voice(voice)
            if not name:
                return {"error": (
                    f"unknown voice {voice!r} — available voices: "
                    f"{', '.join(voice_config.GEMINI_VOICES)}")}
            previous = voice_config.current_voice()
            if name == previous:
                return {"verb": "set_voice", "voice": name,
                        "note": "already the active voice"}
            try:
                voice_config.set_voice(name)
            except Exception as exc:
                return {"error": f"could not save the voice preference: {exc}"}
            return {"verb": "set_voice", "voice": name, "previous": previous,
                    "note": ("voice saved — a live voice session applies it "
                             "on reconnect")}
        return {"verb": action,
                "current_voice": voice_config.current_voice(),
                "voices": list(voice_config.GEMINI_VOICES)}

    VOCAB_DOC_KEY = "vocab/log"

    async def vocab_note(
        self, term: str, correct: str, note: str | None = None
    ) -> dict[str, Any]:
        """Retired surface name (tools-merge-memory) — kept as the direct
        call form of ada_remember kind='vocab'; the alias shim maps
        term/correct onto `text` for execute()-routed calls."""
        term, correct = (term or "").strip(), (correct or "").strip()
        if not term or not correct:
            raise ValueError("vocab_note requires term and correct")
        return await self._vocab_append(f"{term} → {correct}", note)

    async def _vocab_append(
        self, text: Any, note: Any = None
    ) -> dict[str, Any]:
        """Append a term-coaching entry to the current speaker's personal
        vocab log (vocab/log in their own personal bank — KK's notes land in
        personal-kk, Tony's in personal-tony). Not confirmation-gated:
        append-only, scoped to the caller's own bank."""
        if self.mddb is None:
            raise PermissionError(
                "vocab notes are unavailable on this instance")
        bank = memory_ops.persona_bank_for(self.banks, self._memory_identity())
        if bank is None:
            raise PermissionError(
                "vocab notes need a personal bank — this identity has none"
            )
        entry = str(text or "").strip()
        note_s = str(note).strip() if note else ""
        if not entry:
            raise ValueError("vocab memory requires text ('term → correction')")
        today = datetime.now(timezone.utc).date().isoformat()
        line = f"- {entry} — {today}" + (f" ({note_s})" if note_s else "")
        doc = await self.mddb.get_document(bank.mddb_collection, self.VOCAB_DOC_KEY)
        body = ((doc or {}).get("contentMd") or doc and doc.get("content_md") or "")
        if not body.strip():
            body = "# Vocabulary — terms I heard, gently corrected\n"
        if line not in body:
            body = body.rstrip("\n") + "\n" + line + "\n"
        meta = {
            "kind": ["vocab"], "subject": ["persona"],
            "status": ["active"], "scope": ["instance"],
            "last_verified": [today],
        }
        if doc is None:
            ok = await self.mddb.add_document(
                bank.mddb_collection, self.VOCAB_DOC_KEY, "en", body, meta)
        else:
            ok = await self.mddb.update_document(
                bank.mddb_collection, self.VOCAB_DOC_KEY, content_md=body, meta=meta)
        if not ok:
            return {"status": "error", "error": "mddb write failed"}
        return {"status": "noted", "bank": bank.name, "key": self.VOCAB_DOC_KEY,
                "entry": entry}

    def _persona_admin(self, caller: str | None) -> bool:
        """Full-access identities may manage other people's profiles.

        'admin' is the name the master ADA_API_KEY resolves to; otherwise a
        {full: true} entry in person_policies or control_policies grants it
        (same convention as actuation admin)."""
        if not caller:
            return False
        if caller == "admin":
            return True
        for policies in (self.banks.person_policies, self.banks.control_policies):
            if (policies.get(caller) or {}).get("full"):
                return True
        return False

    async def _resolve_persona_target(self, person: str, caller: str | None) -> str:
        """Resolve person ('KK' or 'person.kk') to an entity id and check
        the caller may touch that profile."""
        resolved = await self.context.ha_client.resolve_person(person)
        if resolved is None:
            try:
                known = ", ".join(
                    p["entity_id"] for p in await self.context.ha_client.persons()
                ) or "none"
            except Exception:
                known = "unavailable (Home Assistant unreachable)"
            raise ValueError(
                f"no Home Assistant person matches {person!r} (known: {known})"
            )
        target = resolved["entity_id"]
        if target == caller:
            return target
        # Self-targeting by name still counts as self when both identities
        # land in the same persona bank (e.g. key 'user-kk' -> person.kk).
        if caller:
            own = memory_ops.persona_bank_for(self.banks, caller)
            theirs = memory_ops.persona_bank_for(self.banks, target)
            if own is not None and own is theirs:
                return target
        if not self._persona_admin(caller):
            logger.warning(
                "denied persona manage of %s for identity %r", target, caller
            )
            raise PermissionError(
                "managing another person's profile requires a full-access "
                f"identity (caller: {caller or 'anonymous'})"
            )
        return target

    async def _persona_list(self) -> dict[str, Any]:
        """List HA person entities with their persona bank + custom knobs."""
        people = await self.context.ha_client.persons()
        out = []
        for p in people:
            entry = dict(p)
            if self.mddb is not None:
                bank = memory_ops.persona_bank_for(self.banks, p["entity_id"])
                entry["persona_bank"] = bank.name if bank else None
                persona = await memory_ops.get_persona(
                    self.mddb, self.banks, p["entity_id"]
                )
                entry["custom_knobs"] = persona["custom"]
            out.append(entry)
        return {"verb": "list", "persons": out}

    async def ada_forget(self, bank: str, key: str, reason: str | None = None) -> dict[str, Any]:
        """Retract a memory: status becomes retracted; the doc stays auditable."""
        return await memory_ops.forget(
            self.mddb, self.banks, bank, key, reason, session_id=self.session_id,
            person_entity=self._memory_identity(),
        )

    async def ada_enroll_speaker(
        self,
        name: str,
        ha_person: str | None = None,
        display_name: str | None = None,
        confirmed: bool = False,
        who: str = "speaker",
    ) -> dict[str, Any]:
        """Enroll the current speaker's voice from buffered audio.

        Uses the unrecognized-speaker voice accrued across the session
        (or the last ~15s of audio if they were already identified) —
        no separate recording needed. Call this when the user asks to
        enroll their voice or when Ada offers enrollment.
        """
        # who='guest' absorbed chaba's guest_register (tools-merge-
        # meta-voice): a plain name registration for later admin
        # promotion — no voice buffer or speaker gating involved.
        if str(who or "speaker").strip().lower() == "guest":
            return await self.guest_register(str(name))
        speaker_session = _CALLER_SPEAKER_SESSION.get() or self.speaker_session
        if not ha_person:
            # Auto-resolve 'Name' -> person.<slug> so the enrollment maps to
            # the speaker's HA person (memory banks + actuation ACL follow)
            # even when the model doesn't pass ha_person explicitly.
            try:
                resolved = await self.context.ha_client.resolve_person(str(name))
                if resolved:
                    ha_person = resolved["entity_id"]
            except Exception:
                slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
                if slug:
                    candidate = f"person.{slug}"
                    try:
                        state = await self.context.ha_client.get_state(candidate)
                        if state.get("entity_id") == candidate:
                            ha_person = candidate
                    except Exception:
                        pass
        # Secondary-turn carve-out: while a non-owner voice is identified,
        # only an enrollment targeting the OWNER is allowed — and only if
        # the buffered voice doesn't score as the secondary speaker
        # (otherwise e.g. KK could enroll her own voice under 'Tony').
        if self._is_secondary_turn():
            target = ha_person or f"person.{_slug(name)}"
            if target != self._owner():
                raise PermissionError(
                    "Only the session owner can enroll voices on this "
                    "session — propose it to them aloud and let them ask "
                    "in their own voice.")
            cur = self._current_speaker()
            if cur and not confirmed:
                try:
                    buf_name, _buf_score = speaker_session.identify_buffer()
                    buf_person = (
                        speaker_session._identifier.get_ha_person(buf_name)
                        if buf_name else None)
                except Exception:
                    buf_person = None
                if buf_person and buf_person == cur:
                    raise PermissionError(
                        f"the buffered voice still matches {buf_name}. "
                        "If this is the owner being MISIDENTIFIED (their "
                        "stale print matches another enrolled speaker), "
                        "say so aloud, ask them to confirm the fix "
                        "out loud, then retry with confirmed=true — that "
                        "overwrites the stale profile. Otherwise the "
                        "identified speaker must stop talking first.")
        if speaker_session is None:
            return {
                "error": "speaker identification is not active on this session "
                "(speaker ID may be disabled or not configured)"
            }
        # force replaces a stale/poisoned profile — only ever allowed for
        # the session OWNER's own enrollment, and only after the user
        # explicitly confirms the fix (confirmed=true)
        owner = self._owner()
        target_person = ha_person or f"person.{_slug(name)}"
        force = bool(confirmed) and owner is not None and \
            target_person == owner
        try:
            result = speaker_session.enroll_from_buffer(
                str(name),
                ha_person=ha_person or None,
                display_name=display_name or None,
                force=force,
            )
            out = {
                "status": "enrolled",
                "name": result.get("name"),
                "ha_person": result.get("ha_person"),
                "display_name": result.get("display_name"),
                "duration_s": result.get("duration_s"),
            }
            if force:
                out["note"] = ("profile was force-replaced — if another "
                               "speaker still gets matched to you, they "
                               "may need to re-enroll.")
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.warning("voice enrollment failed: %s", exc)
            return {"error": f"enrollment failed: {exc}"}

    # -- Chaba guest tools (CHABA_MEMORY=1 instances only) ------------------
    # File-backed public memory under ~/.local/share/chaba/. No MDDB, no
    # embeddings, no bank registry — guests write to guests/<name>.yml and
    # promoted users to users/<name>.yml (+ a private file for their own
    # namespace). Identity comes from the websocket session, not the model.

    def _require_chaba(self) -> None:
        if self.chaba is None:
            raise PermissionError("guest tools are only available on CHABA_MEMORY=1 instances")

    async def guest_remember(self, key: str, text: str) -> dict[str, Any]:
        """Save a public memory under this visitor's declared name."""
        self._require_chaba()
        return self.chaba.remember(self.session_id, key, text)

    async def guest_remember_private(self, key: str, text: str) -> dict[str, Any]:
        """Save a private note — only for admin-promoted users."""
        self._require_chaba()
        return self.chaba.remember_private(self.session_id, key, text)

    async def guest_recall(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search public guest memories (and own private notes for users)."""
        self._require_chaba()
        return {"hits": self.chaba.recall(query, session_id=self.session_id, limit=limit)}

    async def guest_register(self, name: str) -> dict[str, Any]:
        """Retired surface name (tools-merge-meta-voice) — kept as the
        direct call form of ada_enroll_speaker who='guest'; the alias shim
        adds who='guest' for execute()-routed calls."""
        self._require_chaba()
        return self.chaba.register_pending(name, session_id=self.session_id)
