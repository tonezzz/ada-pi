# dispatch-outcome: ada-transcript-durability

**Card:** ada-transcript-durability — fleet restart at 21:24:11 killed session
4585f3b060 mid-conversation; transcript never flushed (writes only on clean
close).
**Commit:** `ad11f44` on `dispatch/20261014-220023-fleet-restart-at-21-24-11-kill` (not pushed).

## What changed

1. **Incremental transcript append** (option 1 from the card — SIGKILL-safe,
   chosen over the SIGTERM handler):
   `backend/conversation_memory.py` — `ConversationMemory` gained a
   `durable_transcript` flag; when on, `add_user` / `add_assistant` /
   `add_channel_mirror` append each completed turn to the archive file
   (`<day>-<sid>.md`, ref comment + session header written on first append) as
   it lands. `_save_transcript_file` still rewrites the canonical file at clean
   close; the archive path is memoized at first write so a midnight-crossing
   session stays in one file. The flag defaults off and `no_persist` still
   gates it, so tests and `tests/scenario_engine.py` never write into the real
   `~/.local/share/ada/transcripts` dir.
   `pwa_server.py` — the `/ws` handler sets `conversation.durable_transcript =
   not no_persist`, covering voice, text-mode, and relay sessions uniformly.
2. **Drain grace in deploy-ada.sh** — `pwa_server.py` `/api/health` now reports
   `active_sessions` (count only, endpoint stays unauthenticated);
   `scripts/deploy-ada.sh` gained a step-4 drain gate between the test gate and
   the restart: for each unit it resolves the port (ADA_PORT in the unit
   environment, else `--port` in ExecStart), polls `/api/health`, and waits up
   to `ADA_DRAIN_S` (default 45s; `0` skips) for live sessions to end. Expiry
   restarts anyway — the wait protects mid-flight replies; transcripts are
   already durable. Units without a port/listener (relays, down services) are
   skipped as drained.
3. **Tests** — `tests/test_context_awareness.py::TranscriptFileTests`: three
   new cases (append at add-time incl. mirrored turns, default-off,
   no_persist).
4. **Docs** — `docs/ssot/jobs/ada/2026-10-14-transcript-durability.yml`;
   AGENTS.md deploy-gates line now mentions the drain step.

## Result / verification

- `python3 -m py_compile` clean on all touched files; `bash -n` clean on
  deploy-ada.sh.
- Stubbed smoke run (bare interpreter has no httpx/pytest): durable session
  writes the archive file on the first `add_user` without `persist()`, mirrors
  land, canonical rewrite still works; default and `no_persist` sessions write
  nothing. Port-env, `--port`, and health-JSON parsing verified.
- Not run: the full `pytest tests/` gate — no `.venv` or pytest/httpx in this
  worktree; the deploy gates will run it on idc03.
- Verify in prod: `curl -s http://127.0.0.1:8002/api/health | python3 -c
  'import json,sys;print(json.load(sys.stdin)["active_sessions"])'`; deploy
  with `ADA_DRAIN_S=5` to see the drain lines; kill a live session and confirm
  `~/.local/share/ada/transcripts/<day>-<sid>.md` already holds every completed
  turn.
