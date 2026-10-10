# Dispatch outcome — ada-phantom-card-claims (20261010-010520)

**Task**: Ada phantom-writes — narrates kanban cards that were never filed
(session 67b02417a8: "เปิดการ์ดให้ Devin…เรียบร้อยแล้ว อยู่ใน Doing" and
"บันทึกไว้บนบอร์ดแล้ว" with zero file calls and no cards on the board).

## Root cause — why `phantom_write_claim` never fired

Two holes in the detector at `backend/realtime_provider.py`:

1. **Vocabulary**: `_PHANTOM_CLAIM_RE` had save/screen verbs but no board
   words — "เปิดการ์ด…เรียบร้อยแล้ว" matched nothing at all.
2. **Whitewash gate**: the event fired only when the turn had *zero* tool
   calls or a *failed* result — ANY successful call (a kanban `list`, a
   `memory_search`) counted as cover for a narrated write. "บันทึกไว้"
   likely matched the regex but was silenced by a passed read in the same
   turn.

A third enabler: `kanban action=file` returned `ok` on any board-api 2xx
without confirming a card landed, and never surfaced the card id — so a
narration had no checkable fact and a silently-dropped write could still
read as success.

## What changed

- `backend/realtime_provider.py`
  - `_PHANTOM_CLAIM_RE`: added `filed` to the verb set; EN
    `(posted|dropped|added|put|logged|raised|moved|created|made) …
    (card|kanban|board)`; Thai `(เปิด|สร้าง|เพิ่ม|ย้าย|ปิด|ลง)การ์ด…แล้ว`,
    `การ์ด…เรียบร้อย/เสร็จแล้ว`, `ลง/ขึ้นบอร์ด…แล้ว`, `บันทึก…บอร์ด`.
    Bare state reports ("อยู่ใน doing", EN "opened the card") stay out —
    honest when backed by a read.
  - New `_claim_backing_call(name, args)` + `_CLAIM_BACKING_TOOLS` /
    `_CLAIM_BACKING_ACTIONS`: only ok results from mutating seats back a
    claim (kanban file/comment/move/ask/respond; tasks add/done/move;
    ada_ops outcome; cms_edit; docs archive/print; drive update; yt
    cast/stop; cast_to_screen actuating actions; ada_camera_snapshot
    display-push; whole-tool writers/actuators).
  - `events()`: tracks `backing_calls_this_turn`; gate is now
    `claim AND (backing==0 OR failed>0)` — a passed read no longer
    whitewashes, and "saved" over a denied write still fires.
  - Instructions: DONE MEANS DONE gains a board clause (claim only on
    this-turn `ok`, speak the card id; a write that didn't land must be
    said aloud); request-capture rule says "on the board" only after the
    tool returns ok.
- `backend/tools.d/kanban.py` — `_file` verify-after-write: after
  `POST /card`, `GET /cards` must resolve the card (parsed id →
  title-slug fallback) or the tool returns `ok:False` ("did not file").
  Result carries `id` and a note telling Ada to speak it. A failed verify
  read after a real write keeps `ok` + `warning` (never a false failure).
- `tests/test_kanban.py` — FileTest: verified id, absent-card honest
  failure, unverifiable-read warning, title-slug fallback.
- `tests/test_provider_events.py` — board claim/non-claim phrases (both
  observed phantom lines), `ClaimBackingCallTests`, and
  `PhantomWriteClaimGateTests` driving `events()` end-to-end: read-only
  call fires, zero calls fire, failed write fires, ok write backs.

Trail: `docs/ssot/jobs/ada/2026-10-10-phantom-card-claims.yml`.

## Result / how to verify

- `python -m unittest tests.test_kanban tests.test_provider_events` — 91 green.
- Full `unittest discover` — 983 tests; only pre-existing/environmental
  failures (test_devteam needs pytest, absent from the ada-pi venv; the two
  `test_tool_audit` lint tests are CI-deselected and flag identical
  voice_fx/yt_cached_list debt on base — verified via stash diff of
  `scripts/tool-lint.py`).
- Live check post-deploy: provoke "put X on the board" with the board
  unreachable → Ada must say the write didn't land; ops feed should show
  `phantom_write_claim` if she claims a card over a read-only turn.

Not committed/pushed per dispatch rules — changes are in this worktree on
`dispatch/20261010-010520-ada-phantom-writes-narrates-ka`.
