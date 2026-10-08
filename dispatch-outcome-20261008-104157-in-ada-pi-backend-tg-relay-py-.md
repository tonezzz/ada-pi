# Dispatch outcome — tg voice corpus capture

## Result

`backend/tg_relay.py` now captures `message.voice`, `message.audio`, and
`message.video_note` from enrolled senders into a local voice corpus:

- **Consent gate**: the media branch sits *after* the relay's existing
  authorization — `TELEGRAM_ALLOWED_CHAT_IDS` for the chat and
  `TELEGRAM_USER_CALLERS` per-sender in group rooms. Unknown chats and
  unmapped room members return early; nothing touches disk.
- **Download**: `getFile` + file-host fetch, same two-step as photos —
  `_fetch_photo` was refactored onto a shared `_download_file()`.
- **Storage**: `TG_VOICE_CORPUS_DIR` env (default
  `~/.local/share/ada-voice-corpus/tg`, user share dir, not the repo) →
  `<sender_slug>/<YYYYmmdd-HHMMSS>.<ext>` — `.ogg` for voice, `.mp4` for
  video_note, getFile-suffix/`_MIME_EXT` for audio. Sender slug = caller
  key name (e.g. `user-tony`) → username → first_name → numeric id,
  sanitized. Same-second collisions get `-1`, `-2`, … suffixes.
- **Manifest**: one JSONL line per clip in `manifest.jsonl` at the
  corpus root — `{ts, sender, sender_id, chat_id, kind, duration_s,
  file_id, mime, path}` (`kind` added over the card's field list).
- **Ack**: "Saved Ns voice clip to the voice corpus." (or a failure
  notice). Clips never reach Ada — no session is opened, no
  forwarding, no transcription.

## Files changed

- `backend/tg_relay.py` — `VOICE_CORPUS_DIR` env, `_download_file`
  extraction, `_sender_slug` + `_MIME_EXT` helpers, `_save_voice_clip`,
  media branch in `handle()`, docstring env entry.
- `tests/test_tg_relay_voice.py` — new, 6 cases (save+ack+manifest,
  audio/video_note kinds & extensions, same-second no-clobber, group
  caller slug, unauthorized chat saves nothing, unmapped group sender
  saves nothing).
- `docs/ssot/jobs/ada/2026-10-08-tg-voice-corpus-capture.yml` — job
  trail.

## Verify

```
~/CascadeProjects/ada-pi/.venv/bin/python -m pytest tests/test_tg_relay_voice.py -q   # 6 passed
~/CascadeProjects/ada-pi/.venv/bin/python -m pytest tests/ -x -q                       # 904 passed, 1 skipped
```

(System `python3` lacks `google.genai` and fails collection on
unrelated modules — use the repo venv.)

Live check after deploy: send a voice note from an allowed chat →
ack + `~/.local/share/ada-voice-corpus/tg/<slug>/*.ogg` +
`manifest.jsonl` line. Send from an unmapped group sender → refusal,
no file.
