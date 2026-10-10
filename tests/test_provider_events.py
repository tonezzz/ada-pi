import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from google.genai import types

from backend.realtime_provider import (
    GeminiLiveProvider, _claim_backing_call, _media_grounding_problems,
    _phantom_claim)


class InterruptedSession:
    def __init__(self, provider: GeminiLiveProvider) -> None:
        self.provider = provider

    async def receive(self):
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                interrupted=True,
                model_turn=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=b"stale", mime_type="audio/pcm;rate=24000"))]
                ),
                output_transcription=types.Transcription(text="stale transcript"),
            )
        )
        self.provider._closed = True


class ExpressionToolSession:
    def __init__(self, provider: GeminiLiveProvider) -> None:
        self.provider = provider
        self.responses = []

    async def receive(self):
        yield types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(
                    id="expression-call-1",
                    name="set_facial_expression",
                    args={"expression": "sassy"},
                )
            ])
        )
        self.provider._closed = True

    async def send_tool_response(self, *, function_responses):
        self.responses.extend(function_responses)


class HabitObservationToolSession(ExpressionToolSession):
    async def receive(self):
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="habit-1",name="report_habit_observation",args={"challenge_id":"water-1","habit_key":"not_drinking_enough_water","observed":True,"confidence":.9,"reason":"visible drink"})
        ]))
        self.provider._closed=True


class HabitObservationCanonicalToolSession(ExpressionToolSession):
    # Canonical form after tools-merge-memory: ada_remember kind='habit'
    # must emit the same habit_observation event as the retired name.
    async def receive(self):
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="habit-2",name="ada_remember",args={"kind":"habit","challenge_id":"water-2","habit_key":"not_drinking_enough_water","observed":False,"confidence":.7,"reason":"no drink visible"})
        ]))
        self.provider._closed=True


class HabitStatusToolSession(ExpressionToolSession):
    async def receive(self):
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="habit-status-1",name="get_habit_status",args={})
        ]))
        self.provider._closed=True


class HomeStatusToolSession(ExpressionToolSession):
    # Canonical form after tools-merge-tasks-status: home_status
    # what='habit' must return the same snapshot the retired
    # get_habit_status produced.
    async def receive(self):
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="hs-1",name="home_status",args={"what":"habit"})
        ]))
        self.provider._closed=True


class DocCardToolSession(ExpressionToolSession):
    # doc_upload_card_action is a forward alias into chat_send(doc='card')
    # whose own action=/intake_key= args need the surrogate remap
    # (action->op, intake_key->key) BEFORE runner.execute — the provider
    # resolves the name to canonical, so the runner never sees the alias.
    async def receive(self):
        yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="doc-1",name="doc_upload_card_action",args={"action":"archive","intake_key":"doc-abc"})
        ]))
        self.provider._closed=True


class UsageMetadataSession:
    """Yields one message carrying usage_metadata — the provider must
    forward it as a 'usage' event so scenario-live can assert the
    context ceiling (card ada-context-budget)."""

    def __init__(self, provider: GeminiLiveProvider) -> None:
        self.provider = provider

    async def receive(self):
        yield types.LiveServerMessage(
            usage_metadata=types.UsageMetadata(
                prompt_token_count=1234,
                response_token_count=56,
            )
        )
        self.provider._closed = True


class ProviderEventTests(unittest.IsolatedAsyncioTestCase):
    def test_live_config_guards_long_full_duplex_sessions(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "backend/realtime_provider.py").read_text()
        self.assertIn("START_SENSITIVITY_LOW", source)
        self.assertIn('"prefix_padding_ms": 200', source)
        self.assertIn("ActivityHandling.START_OF_ACTIVITY_INTERRUPTS", source)
        self.assertIn('"silence_duration_ms": 500', source)
        self.assertIn("ContextWindowCompressionConfig", source)
        self.assertIn("SlidingWindow", source)
        self.assertIn("SessionResumptionConfig", source)
        self.assertIn("session_resumption_update", source)
        self.assertIn("message.go_away", source)

    async def test_usage_metadata_emits_usage_event(self) -> None:
        provider = GeminiLiveProvider()
        provider._session = UsageMetadataSession(provider)
        events = [event async for event in provider.events()]
        usage = [e for e in events if e.type == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(
            usage[0].data, {"in": 1234, "out": 56, "total_in": 1234})

    async def test_interruption_discards_coalesced_stale_audio(self) -> None:
        provider = GeminiLiveProvider()
        provider._session = InterruptedSession(provider)
        events = [event async for event in provider.events()]
        self.assertEqual([event.type for event in events], ["response_interrupted"])

    async def test_expression_tool_is_forwarded_and_spoken_reply_can_continue(self) -> None:
        provider = GeminiLiveProvider()
        session = ExpressionToolSession(provider)
        provider._session = session

        events = [event async for event in provider.events()]

        self.assertEqual(
            [event.type for event in events],
            ["tool_call", "expression", "tool_result"],
        )
        self.assertEqual(events[0].data["name"], "set_facial_expression")
        self.assertEqual(events[1].data, {"name": "sassy"})
        self.assertEqual(len(session.responses), 1)
        response = session.responses[0]
        self.assertEqual(response.id, "expression-call-1")
        self.assertEqual(response.scheduling, types.FunctionResponseScheduling.WHEN_IDLE)

    async def test_habit_observation_tool_emits_structured_event(self) -> None:
        provider=GeminiLiveProvider(); session=HabitObservationToolSession(provider); provider._session=session
        events=[event async for event in provider.events()]
        self.assertEqual(events[0].type,"tool_call")
        self.assertEqual(events[1].type,"habit_observation")
        self.assertEqual(events[1].data["challenge_id"],"water-1")
        self.assertTrue(events[1].data["observed"])
        self.assertEqual(session.responses[0].name,"report_habit_observation")

    async def test_habit_observation_via_canonical_ada_remember(self) -> None:
        provider=GeminiLiveProvider(); session=HabitObservationCanonicalToolSession(provider); provider._session=session
        events=[event async for event in provider.events()]
        self.assertEqual(events[0].type,"tool_call")
        self.assertEqual(events[1].type,"habit_observation")
        self.assertEqual(events[1].data["challenge_id"],"water-2")
        self.assertFalse(events[1].data["observed"])
        self.assertEqual(session.responses[0].name,"ada_remember")

    async def test_habit_status_tool_returns_complete_snapshot(self) -> None:
        snapshot={"window_days":7,"habits":[{"habit_key":"posture","lifecycle_status":"possible"}]}
        provider=GeminiLiveProvider(habit_state_getter=lambda:snapshot)
        session=HabitStatusToolSession(provider); provider._session=session
        events=[event async for event in provider.events()]
        self.assertEqual([e.type for e in events],["tool_call","tool_result"])
        self.assertEqual(session.responses[0].name,"get_habit_status")
        self.assertEqual(session.responses[0].response,{"ok":True,"output":snapshot})

    async def test_home_status_canonical_returns_habit_snapshot(self) -> None:
        snapshot={"window_days":7,"habits":[{"habit_key":"posture","lifecycle_status":"possible"}]}
        provider=GeminiLiveProvider(habit_state_getter=lambda:snapshot)
        session=HomeStatusToolSession(provider); provider._session=session
        events=[event async for event in provider.events()]
        self.assertEqual([e.type for e in events],["tool_call","tool_result"])
        self.assertEqual(session.responses[0].name,"home_status")
        self.assertEqual(session.responses[0].response,{"ok":True,"output":snapshot})

    async def test_doc_card_alias_remaps_args_before_dispatch(self) -> None:
        provider=GeminiLiveProvider()
        session=DocCardToolSession(provider); provider._session=session
        events=[event async for event in provider.events()]
        self.assertEqual(events[0].type,"tool_call")
        self.assertEqual(events[0].data["name"],"chat_send")
        args=events[0].data["args"]
        self.assertEqual(args.get("doc"),"card")
        self.assertEqual(args.get("op"),"archive")
        self.assertEqual(args.get("key"),"doc-abc")
        self.assertNotIn("action",args)
        self.assertNotIn("intake_key",args)
        self.assertEqual(session.responses[0].name,"doc_upload_card_action")

    async def test_video_frame_uses_live_video_input(self) -> None:
        class VideoSession:
            def __init__(self) -> None:
                self.video = None

            async def send_realtime_input(self, *, video):
                self.video = video

        provider = GeminiLiveProvider()
        session = VideoSession()
        provider._session = session

        await provider.send_video(b"jpeg-frame")

        self.assertEqual(session.video.data, b"jpeg-frame")
        self.assertEqual(session.video.mime_type, "image/jpeg")

    async def test_habit_alert_sends_text_and_current_image_as_a_turn(self) -> None:
        class AlertSession:
            async def send_client_content(self, **kwargs):
                self.payload = kwargs

        provider = GeminiLiveProvider()
        session = AlertSession()
        provider._session = session
        await provider.send_habit_alert(b"current-frame", "first possible habit")
        self.assertTrue(session.payload["turn_complete"])
        parts = session.payload["turns"].parts
        self.assertEqual(parts[0].text, "first possible habit")
        self.assertEqual(parts[1].inline_data.data, b"current-frame")
        self.assertEqual(parts[1].inline_data.mime_type, "image/jpeg")

    async def test_text_turn_can_trigger_a_boot_greeting(self) -> None:
        class TextSession:
            async def send_client_content(self, **kwargs):
                self.payload = kwargs

        provider = GeminiLiveProvider()
        session = TextSession()
        provider._session = session
        await provider.send_text_turn("Give a short greeting")
        self.assertTrue(session.payload["turn_complete"])
        self.assertEqual(session.payload["turns"].parts[0].text, "Give a short greeting")


class UserConfirmedGateTests(unittest.TestCase):
    """_user_confirmed must treat affirmative words inside a longer write
    request as content, not consent."""

    def setUp(self) -> None:
        self.provider = GeminiLiveProvider()

    def test_affirmative_word_inside_write_request_is_not_consent(self) -> None:
        text = (
            "Save a note to my tony-projects bank: marker zeta-gate — "
            "the lab stack design is approved."
        )
        self.assertFalse(self.provider._user_confirmed(text))

    def test_long_request_with_trailing_content_words_not_consent(self) -> None:
        text = (
            "Remember in tony-projects that the benchmark results were "
            "absolutely fine and we should proceed tomorrow."
        )
        self.assertFalse(self.provider._user_confirmed(text))

    def test_short_standalone_affirmation(self) -> None:
        for text in ("yes", "yes go ahead", "sure", "ตกลง"):
            self.assertTrue(self.provider._user_confirmed(text), text)

    def test_leading_affirmation_in_longer_turn(self) -> None:
        text = (
            "yes go ahead and also remember the file upload design we "
            "discussed this morning please"
        )
        self.assertTrue(self.provider._user_confirmed(text))

    def test_no_affirmation(self) -> None:
        self.assertFalse(self.provider._user_confirmed("save that note"))
        self.assertFalse(self.provider._user_confirmed(""))

    # Vetoes — real divergences from jev-corpus (2026-10-04 report).

    def test_explicit_negation_is_not_consent(self) -> None:
        for text in (
            "no",
            "no wait",
            "no, actually don't",
            "เอ๊ะ! No. อือ อือ",
            "ไม่เอา",
            "อย่าเพิ่ง",
            "ยกเลิก",
        ):
            self.assertFalse(self.provider._user_confirmed(text), text)

    def test_approval_question_is_not_consent(self) -> None:
        for text in (
            "อย่างงั้น ผม อนุมัติ ได้ เลย ไหม",
            "should I publish it, yes?",
            "เอาเลยมั้ย",
        ):
            self.assertFalse(self.provider._user_confirmed(text), text)

    def test_confirm_verb_with_object_is_not_consent(self) -> None:
        self.assertFalse(self.provider._user_confirmed(
            "Confirm the camera actually moved — ask it for the current view state."))
        self.assertFalse(self.provider._user_confirmed(
            "Confirm — what is it tracking right now? Ask the map."))

    def test_no_need_grant_still_confirms(self) -> None:
        # "go ahead, no need to wait for my confirmation" — ไม่ต้อง is
        # granting, not denying.
        self.assertTrue(self.provider._user_confirmed(
            "ต่อได้เลยไม่ต้องรอผมยืนยัน"))

    def test_affirmation_before_commentary_still_confirms(self) -> None:
        # "yes" opens the turn; the trailing "don't" is commentary.
        self.assertTrue(self.provider._user_confirmed(
            "Yes, replace it — I don't need the wall right now."))


class ConfirmRetryPendingTests(unittest.TestCase):
    """2026-10-07 ada_remember flake: the model resends confirmed=true on
    the identical denied call instead of replaying confirm_token. When a
    live token covers that exact call the resend IS the replay — the
    provider passes confirmed through instead of stripping it."""

    def test_envelope_keys_filtered_before_fingerprint(self) -> None:
        runner = MagicMock()
        runner.pending_confirm.return_value = True
        provider = GeminiLiveProvider(tool_runner=runner)
        args = {"bank": "personal", "text": "x", "subject": "s",
                "confirmed": True, "confirm_token": "cfm-abc",
                "_verified_affirm": True}
        self.assertTrue(provider._confirm_retry_pending("ada_remember", args))
        runner.pending_confirm.assert_called_once_with(
            "ada_remember",
            {"bank": "personal", "text": "x", "subject": "s"})

    def test_no_pending_token_means_strip_path(self) -> None:
        runner = MagicMock()
        runner.pending_confirm.return_value = False
        provider = GeminiLiveProvider(tool_runner=runner)
        self.assertFalse(provider._confirm_retry_pending(
            "ada_remember", {"bank": "personal", "text": "x"}))

    def test_no_runner_or_runner_error_fails_closed(self) -> None:
        provider = GeminiLiveProvider()
        self.assertFalse(provider._confirm_retry_pending(
            "ada_remember", {"bank": "personal"}))
        runner = MagicMock()
        runner.pending_confirm.side_effect = RuntimeError("boom")
        provider = GeminiLiveProvider(tool_runner=runner)
        self.assertFalse(provider._confirm_retry_pending(
            "ada_remember", {"bank": "personal"}))


class PhantomClaimRegexTests(unittest.TestCase):
    """Write-claim detection must catch the exact verbs Ada narrated over
    failed results ('saved', 'บันทึกแล้ว') — the 2026-10-07 flake narrated
    success on six NOT-EXECUTED ada_remember denials undetected."""

    def test_save_verbs_are_claims(self) -> None:
        for text in (
            "Done — I saved it to your personal bank.",
            "It's stored under gate-remote now.",
            "บันทึกแล้วครับ",
            "จำไว้แล้ว",
        ):
            self.assertTrue(_phantom_claim(text), text)

    def test_board_write_claims(self) -> None:
        """Card ada-phantom-card-claims (session 67b02417a8): both
        narrated phantom lines must trip the detector now."""
        for text in (
            "Done — I filed a card for it.",
            "I posted a card on the board.",
            "Moved the card to doing.",
            "I put that on the board.",
            "เปิดการ์ดให้ Devin จัดการเรื่องฟังเสียง เรียบร้อยแล้ว อยู่ใน Doing",
            "เรื่องปฏิทินได้บันทึกไว้บนบอร์ดแล้ว",
            "ลงบอร์ดแล้วครับ",
        ):
            self.assertTrue(_phantom_claim(text), text)

    def test_board_non_claims(self) -> None:
        for text in (
            "I couldn't file the card — the board is down.",
            "I can file a card for it if you want.",
            "เดี๋ยวจะเปิดการ์ดให้นะ",          # intent, no done marker
            "การ์ดใบนี้อยู่ใน doing ตั้งแต่เมื่อวาน",  # read-backed state
            "I opened the card — it says low priority.",
        ):
            self.assertFalse(_phantom_claim(text), text)

    def test_negated_save_is_not_a_claim(self) -> None:
        self.assertFalse(_phantom_claim(
            "I couldn't get it saved — the write was refused."))
        self.assertFalse(_phantom_claim(
            "I can't save it right now."))

    def test_present_tense_intent_is_not_a_claim(self) -> None:
        self.assertFalse(_phantom_claim("I'll save that for you now."))


class ClaimBackingCallTests(unittest.TestCase):
    """Only a mutating/actuating call may back a narrated write claim —
    a passed read must not whitewash it (card ada-phantom-card-claims)."""

    def test_board_writes_back_a_claim(self) -> None:
        for action in ("file", "create", "comment", "move", "ask",
                       "respond"):
            self.assertTrue(
                _claim_backing_call("kanban", {"action": action}), action)
        self.assertTrue(_claim_backing_call("ada_remember", {}))
        self.assertTrue(_claim_backing_call("cms_publish_page", {}))
        self.assertTrue(_claim_backing_call("devin", {"action": "dispatch"}))
        self.assertTrue(
            _claim_backing_call("cast_to_screen", {"action": "cast"}))

    def test_reads_never_back_a_claim(self) -> None:
        for name, args in (
            ("kanban", {"action": "list"}),
            ("kanban", {"action": "read"}),
            ("tasks", {"action": "list"}),
            ("ada_ops", {"action": "research"}),
            ("docs", {"action": "get"}),
            ("drive", {"action": "search"}),
            ("yt", {"action": "status"}),
            ("cast_to_screen", {"action": "list"}),
            ("ada_memory_search", {"query": "x"}),
            ("web_search", {"query": "x"}),
            ("get_home_state", {}),
            ("set_facial_expression", {"expression": "sassy"}),
            ("ada_camera_snapshot", {"camera": "front"}),
        ):
            self.assertFalse(_claim_backing_call(name, args), name)

    def test_camera_push_to_display_is_backing(self) -> None:
        self.assertTrue(
            _claim_backing_call("ada_camera_snapshot", {"screen": 2}))


class PhantomKanbanTurnSession:
    """One kanban call (faked through the runner) then a turn that
    narrates a filed card — whether the ops event fires depends on
    whether the call's action can back the claim."""

    def __init__(self, provider, action):
        self.provider = provider
        self.action = action
        self.responses = []
        self._sent_call = False

    async def receive(self):
        if not self._sent_call:
            self._sent_call = True
            if self.action is not None:
                yield types.LiveServerMessage(
                    tool_call=types.LiveServerToolCall(function_calls=[
                        types.FunctionCall(
                            id="kb-1", name="kanban",
                            args={"action": self.action})]))
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                output_transcription=types.Transcription(
                    text="Done — I filed a card for it on the board."),
                turn_complete=True))
        self.provider._closed = True

    async def send_tool_response(self, *, function_responses):
        self.responses.extend(function_responses)


class PhantomWriteClaimGateTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, action, result):
        runner = MagicMock()
        runner.execute = AsyncMock(return_value=result)
        provider = GeminiLiveProvider(tool_runner=runner)
        provider._session = PhantomKanbanTurnSession(provider, action)
        provider._emit_ops_event = MagicMock()
        async for _ in provider.events():
            pass
        return [c.args[0] for c in provider._emit_ops_event.call_args_list
                if c.args and c.args[0] == "phantom_write_claim"]

    async def test_read_call_does_not_back_a_write_claim(self):
        fired = await self._run("list", {"ok": True, "cards": []})
        self.assertEqual(len(fired), 1)

    async def test_zero_call_claim_fires(self):
        fired = await self._run(None, None)
        self.assertEqual(len(fired), 1)

    async def test_write_call_backs_the_claim(self):
        fired = await self._run("file", {"ok": True, "id": "p1"})
        self.assertEqual(fired, [])

    async def test_failed_write_still_fires(self):
        fired = await self._run("file", {"ok": False, "error": "nope"})
        self.assertEqual(len(fired), 1)

class MediaClaimGuardTests(unittest.TestCase):
    """ada-tv-action-hallucinated-input (2026-10-09, transcript
    cfed319d22/73d7713ce8): Ada narrated 'Dancing In The Street' playing
    while the turn's only evidence was cast_verify=paused — the title
    was in no tool result. Playing-state and title claims must cite a
    same-turn tool result."""

    def test_incident_replay_title_over_paused_verify(self):
        problems = _media_grounding_problems(
            "Dancing In The Street is playing on the TV.",
            [("tv_action", {"ok": False, "cast_verify": {
                "ok": False, "state": "paused",
                "entity": "media_player.tony_tv_cast"}})])
        self.assertTrue(any("Dancing In The Street" in p
                            for p in problems), problems)
        self.assertTrue(any("playing" in p for p in problems), problems)

    def test_thai_title_claim_flagged(self):
        problems = _media_grounding_problems(
            "กำลังเล่นเพลง Dancing In The Street อยู่ค่ะ",
            [("tv_action", {"cast_verify": {"state": "paused"}})])
        self.assertTrue(any("Dancing In The Street" in p
                            for p in problems), problems)

    def test_grounded_answer_is_clean(self):
        # A 'what is playing' answer that cites an actual yt/status
        # result — the contract the card requires.
        results = [("yt", {"ok": True, "state": "playing",
                           "title": "Dancing In The Street"})]
        self.assertEqual(_media_grounding_problems(
            "Dancing In The Street is playing.", results), [])
        self.assertEqual(_media_grounding_problems(
            "It's still playing — 'Blue Monday'.",
            [("yt", {"ok": True, "state": "playing",
                     "title": "Blue Monday"})]), [])

    def test_zero_tool_calls_flags_playing_claim(self):
        problems = _media_grounding_problems("it's playing now.", [])
        self.assertIn("asserted 'playing'", problems)

    def test_error_text_is_not_evidence(self):
        # Our own admonition strings must not launder a claim — a
        # failed result saying "do not claim it is playing" does not
        # ground a playing claim.
        problems = _media_grounding_problems(
            "it's playing.",
            [("tv_action", {"ok": False, "error":
                            "do not claim it is playing"})])
        self.assertIn("asserted 'playing'", problems)

    def test_negated_and_non_media_text_clean(self):
        self.assertEqual(_media_grounding_problems(
            "it's not playing anything.", []), [])
        self.assertEqual(_media_grounding_problems(
            "the page loaded on the TV.", []), [])
        # No play word -> quoted span alone is not a media claim.
        self.assertEqual(_media_grounding_problems(
            "I saved the page 'Flood Report'.", []), [])

    def test_filler_edges_not_titles(self):
        # "now playing on the TV" extracts no bogus title — but the
        # playing claim itself still needs evidence.
        problems = _media_grounding_problems("it's now playing on the TV.", [])
        self.assertEqual(problems, ["asserted 'playing'"])


if __name__ == "__main__":
    unittest.main()
