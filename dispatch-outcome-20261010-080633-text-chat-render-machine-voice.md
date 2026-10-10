# dispatch outcome — machine voice bracket + play button, no autoplay

## What changed

Every machine-voice utterance (flat speechSynthesis TTS — barks,
`notify_voice`, boot voice, text-only fallback) now renders in the text
log as a bracketed `[machine voice] <text>` line with a ▶ replay button.

- `pwa/app.js` — `systemSay` → `machineVoice(text)`:
  `machineVoiceLine()` appends a `▶ [machine voice] <text>` entry to
  `#pwa-log`; `speakMachine()` holds the speechSynthesis call. On this
  live-voice surface the utterance still auto-speaks (card: "live-voice
  surface behaviour unchanged") — the line is the new part: labelled,
  replayable. All 8 call sites renamed; `systemHush` unchanged. Muted
  (`Sound: Off`): the line still renders, auto-speak skipped, but an
  explicit ▶ tap still speaks — deliberate gesture beats the auto-mute.
- `pwa/index.html` — CSS for `.machine-voice` / `.mv-tag` /
  `.machine-voice-play`; `app.js?v=11` → `?v=12`.
- `pwa/cards/ada-chat-card.js` (the PWA text chat, `/chat.html`) —
  `bark` + `notify_voice` were silently dropped before; now they render
  the same bracketed line + ▶. **No autoplay** — the voice only sounds
  when the user presses the button (`_speakMachine()` in the click
  handler — a user gesture, so iOS/Safari unlock is covered).
- `pwa/guest/guest.js` + `pwa/guest/index.html` — guest text chat gets
  the identical non-autoplay treatment.
- `pwa/chat.html` — added `Cache-Control: no-store` meta (was missing;
  index.html and guest/index.html already had it — without it
  `ada-chat-card.js` could be served stale).
- `docs/ssot/jobs/ada/2026-10-10-machine-voice-bracket.yml` — job record
  incl. the live-voice autoplay decision, flagged for review.

LINE/TG relays: confirmed unchanged — `bark`/`notify_voice` are ws-only
events the relays never see; they are text-only already, per the card.

## Result

- Expected-goal checks: all 3 pass (bracket-label, play-control,
  no-default-autoplay — `systemSay(` no longer exists in app.js).
- `node --check` clean on all three edited JS files.
- Stubbed-DOM smoke harness: 20/20 assertions — line text, button
  presence, autoplay on voice surface, silence + on-demand ▶ on both
  text surfaces, muted-render behaviour, hush-before-replay.
- Not deployed (dispatch scope = worktree only). idc03 deploy runs
  `~/.local/bin/deploy-ada.sh` after merge, per AGENTS.md.

## How to verify

1. Open the PWA text chat (`/chat.html`), Connect, then trigger a
   `notify_voice` (non-urgent `/api/notify`) or wait for a bark — expect
   a centered `[machine voice] <text>` line with a ▶ button and total
   silence until it's pressed.
2. On the voice PWA (`/index.html`), a bark/notify both speaks AND
   leaves the bracketed line in the log; ▶ replays it.
3. Ada's normal voice replies still play on the voice surface.

lessons:
- pwa/app.js `systemSay` was the only speechSynthesis path — the
  text-chat surfaces (ada-chat-card.js, guest.js) previously dropped
  bark/notify_voice ws events entirely, so notifications were invisible
  there, not just unlabelled.
- The card's "no autoplay" scopes to the text-chat surface; the brief
  preserves live-voice auto-speak — expected_goal greps accept either
  reading, so record the choice in the job file for review.
- pwa/chat.html lacked the Cache-Control no-store meta the sibling
  pages have — static-mount caching can pin a stale card JS otherwise.
- Function decls in non-strict indirect eval leak to global scope —
  handy for node DOM-stub smoke tests of script-style PWA files.
