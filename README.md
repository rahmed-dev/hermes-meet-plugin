# google_meet (patched override copy)

This is a patched copy of the Hermes Agent built-in `google_meet` plugin (NousResearch/hermes-agent,
`plugins/google_meet`, MIT licence, see LICENSE). Install it as a **user plugin** at
`$HERMES_HOME/plugins/google_meet/`. A user plugin with the same name replaces the built-in copy
(Hermes logs "shadows the bundled copy"), so the Hermes source stays unmodified.

Changes against the built-in plugin:

- Imports are relative, and the bot process runs as `google_meet.meet_bot` from the user plugins
  directory (`process_manager.py` puts that directory on `PYTHONPATH`).
- `on_session_end` no longer leaves the call (Hermes fires it after every agent turn).
- Persistent Chromium profile (`workspace/meetings/chrome-profile`) instead of a static
  `storage_state`, so Google cookie rotation does not sign the bot out; signed-out detection.
- Real Chrome user agent; captions through the "Captions and translation" panel.
- Headed Chromium in its own Xvfb display (stops with the bot).
- Auto-leave: Meet end screen, alone 5 min after others were in the call (20 min if nobody came),
  3 h limit when no duration is given.
- Audio recording (per-bot PulseAudio null sink + ffmpeg) and `meet_transcribe.py`: Groq Whisper
  transcript after the call, caption speaker names merged by time, captions kept as `captions.txt`.

`extras/` holds copies of related files that live elsewhere in the Hermes home (the patched
google-workspace skill with `calendar create --meet/--notify` and owner-as-organizer, the calendar
auto-join watcher, and the Google sign-in script). Hermes does not load them from here.

When Hermes updates its built-in `google_meet`, compare it with this copy and port the changes by hand.

---

# google_meet plugin

Let the hermes agent join a Google Meet call, transcribe it, optionally speak
in it, and do the followup work afterwards.

## What ships

| Version | What | Status |
|---|---|---|
| v1 | Transcribe-only: Playwright joins Meet, scrapes captions to transcript file | ✓ ships by default |
| v2 | Realtime speech out: bot speaks in-call via OpenAI Realtime + BlackHole/PulseAudio null-sink; input stays caption-derived | ✓ opt in with `mode='realtime'` |
| v3 | Remote node host: run the bot on a different machine than the gateway | ✓ opt in with `node='<name>'` |

## Architecture

```
┌─ gateway (Linux box, where hermes runs) ────────────────────────────┐
│                                                                      │
│   agent → meet_join(url, mode='realtime', node='my-mac')             │
│         │                                                            │
│         └─ NodeClient ─── ws ────┐                                   │
│                                  │                                   │
└──────────────────────────────────┼───────────────────────────────────┘
                                   │ wss (token auth)
                                   ▼
┌─ node host (user's Mac, signed-in Chrome lives here) ───────────────┐
│                                                                      │
│   NodeServer (from `hermes meet node run`)                           │
│     │                                                                │
│     ├─ start_bot → process_manager.start() → spawns meet_bot         │
│     │                                                                │
│     └─ meet_bot (Playwright)                                         │
│        ├─ Chromium → meet.google.com                                 │
│        ├─ caption scraper → transcript.txt                           │
│        └─ (realtime mode only) RealtimeSpeaker thread                │
│             ↓                                                        │
│           OpenAI Realtime WS → speaker.pcm                           │
│             ↓                                                        │
│           paplay → null-sink ← Chrome fake mic                       │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Without v3: the whole right column runs on the gateway machine.
Without v2: the "realtime" path is skipped; transcribe runs alone.

## Files

| Path | Purpose |
|---|---|
| `plugin.yaml` | manifest |
| `__init__.py` | `register(ctx)` — registers 5 tools + `on_session_end` hook + `hermes meet` CLI |
| `meet_bot.py` | Playwright bot subprocess (standalone, `python -m plugins.google_meet.meet_bot`) |
| `process_manager.py` | local bot lifecycle + `enqueue_say` |
| `tools.py` | agent-facing tools + node-routing helper |
| `cli.py` | `hermes meet setup / auth / join / status / transcript / say / stop / node ...` |
| `audio_bridge.py` | v2: PulseAudio null-sink (Linux) + BlackHole probe (macOS) |
| `realtime/openai_client.py` | v2: `RealtimeSession` + `RealtimeSpeaker` (file-queue → OpenAI Realtime WS → PCM) |
| `node/protocol.py` | v3: message envelope + validation |
| `node/registry.py` | v3: `$HERMES_HOME/workspace/meetings/nodes.json` |
| `node/server.py` | v3: `NodeServer` (runs on host machine) |
| `node/client.py` | v3: `NodeClient` (used by tool handlers + CLI on gateway) |
| `node/cli.py` | v3: `hermes meet node {run,list,approve,remove,status,ping}` |
| `SKILL.md` | agent usage guide |

## Local quick start

```bash
hermes plugins enable google_meet
hermes meet install                                      # pip + Chromium
hermes meet setup                                        # preflight
hermes meet auth                                         # optional
hermes meet join https://meet.google.com/abc-defg-hij    # transcribe
```

## Realtime mode

Linux (preferred, most automated):
```bash
hermes meet install --realtime                     # installs pulseaudio-utils
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
# then from the agent or CLI:
hermes meet say "Good morning everyone, I'm the note-taker bot."
```

macOS:
```bash
hermes meet install --realtime     # runs: brew install blackhole-2ch ffmpeg
# then — manually! — open System Settings → Sound → Input → BlackHole 2ch
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
```

On macOS, hermes will **not** switch your system audio input automatically — the
user has to do it. This is deliberate: switching default input on a whim would
be a surprising side effect.

Realtime mode is **speak-only**: `speaker.pcm` is streamed into the virtual mic by a
stdin-fed `paplay` / `ffmpeg` pump that follows the file as Realtime appends audio.
Incoming speech is still the caption scrape (v1) — meeting audio is never sent to the
Realtime session, so there is no barge-in on raw audio and no STT billing. After
admission the bot unmutes itself if Meet seated it muted; `status.json` / `hermes meet
status` report the result as `micState` (`unmuted`, `unmuted_clicked`, `unknown`).

## Remote node host

On the node machine (e.g. user's Mac with a signed-in Chrome):
```bash
hermes plugins enable google_meet
hermes meet install
hermes meet node run --display-name my-mac --host 0.0.0.0 --port 18789
# prints the bearer token on first run; copy it
```

On the gateway:
```bash
hermes meet node approve my-mac ws://<mac-ip>:18789 <token>
hermes meet node ping my-mac
# now any meet_* tool call accepts node='my-mac' (or 'auto')
```

## Safety

- URL gate: only `https://meet.google.com/abc-defg-hij`, `/new`, `/lookup/<id>`.
- No calendar scanning, no auto-dial, no auto-consent announcement.
- Node server uses bearer-token auth; no key exchange, no TLS termination
  built in — run it on a LAN or behind a reverse proxy you trust.
- One active meeting per (gateway, node) pair. A second `meet_join` leaves the first.
- `meet_say` refuses unless the active meeting was started with `mode='realtime'`.

## Out of scope

- **Calendar scanning** — deliberately not implemented. Join URLs must be explicit.
- **Multi-tenant node sharing** — a node serves one gateway at a time.
- **Windows** — audio bridging isn't tested; `register()` no-ops on Windows.
- **System audio input switching on macOS** — user responsibility, not the bot's.
- **Meeting-audio ingestion into Realtime** — input is caption-derived; true bidirectional audio is a separate feature.
