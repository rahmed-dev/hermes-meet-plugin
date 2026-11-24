"""Headless Google Meet bot — Playwright + live-caption scraping.

Standalone subprocess spawned by ``process_manager.py``; configured via ``HERMES_MEET_*`` env,
status + transcript written under ``$HERMES_MEET_OUT_DIR`` (filesystem is the only IPC).
No WebRTC audio parsing: Meet's live captions are watched via a MutationObserver — lossy and
English-biased, but deterministic (no STT billing) and stable thanks to the ARIA role.
Debug: ``HERMES_MEET_URL=... HERMES_MEET_OUT_DIR=./meet-out HERMES_MEET_HEADED=1 \\
    PYTHONPATH=$HERMES_HOME/plugins python -m google_meet.meet_bot``
"""

from __future__ import annotations

import atexit  # LOCAL PATCH
import contextlib  # LOCAL PATCH
import json  # LOCAL PATCH
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from hermes_cli.browser_runtime import chromium_executable
from utils import atomic_json_write

# Short three-segment code, a lookup URL, or /new. Anything else is rejected.
MEET_URL_RE = re.compile(
    r"^https://meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}|lookup/[^/?#]+|new)"
    r"(?:[/?#].*)?$")

_FFMPEG_MISSING = "ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS"


def _is_safe_meet_url(url: str) -> bool:
    """True if *url* is a Google Meet URL we're willing to navigate to."""
    return isinstance(url, str) and bool(MEET_URL_RE.match(url.strip()))


def _meeting_id_from_url(url: str) -> str:
    """3-segment meeting code, or a timestamped id for ``/lookup/...`` and ``/new``."""
    m = re.search(r"meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,})", url or "")
    return m.group(1) if m else f"meet-{int(time.time())}"


def _quiet(fn, *args, **kwargs):
    """Call *fn*, swallowing any exception (best-effort teardown steps)."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# status.json keys in file order → _BotState attribute + initial value.
_STATUS_FIELDS = (
    ("meetingId", "meeting_id", None), ("url", "url", None), ("inCall", "in_call", False),
    ("captioning", "captioning", False), ("captionsEnabledAttempted", "captions_enabled_attempted", False),
    ("lobbyWaiting", "lobby_waiting", False), ("joinAttemptedAt", "join_attempted_at", None),
    ("joinedAt", "joined_at", None), ("lastCaptionAt", "last_caption_at", None),
    ("transcriptLines", "transcript_lines", 0), ("transcriptPath", "transcript_path", None),
    ("error", "error", None), ("exited", "exited", False), ("pid", None, None),
    # realtime telemetry
    ("realtime", "realtime", False), ("realtimeReady", "realtime_ready", False),
    ("realtimeDevice", "realtime_device", None), ("audioBytesOut", "audio_bytes_out", 0),
    ("lastAudioOutAt", "last_audio_out_at", None), ("lastBargeInAt", "last_barge_in_at", None),
    ("leaveReason", "leave_reason", None), ("micState", "mic_state", None))


class _BotState:
    """Single-process mutable state, flushed to ``status.json`` on each change."""

    def __init__(self, out_dir: Path, meeting_id: str, url: str):
        self.__dict__.update({attr: default for _, attr, default in _STATUS_FIELDS if attr})
        self.__dict__.update(out_dir=out_dir, meeting_id=meeting_id, url=url, _seen=set(),  # seen "speaker|text"
                             transcript_path=out_dir / "transcript.txt", status_path=out_dir / "status.json")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._flush()

    def record_caption(self, speaker: str, text: str) -> None:
        """Append a caption line unless this exact (speaker, text) was already seen."""
        speaker, text = (speaker or "").strip() or "Unknown", (text or "").strip()
        key = f"{speaker}|{text}"
        if not text or key in self._seen:
            return
        self._seen.add(key)
        self.transcript_lines += 1
        self.last_caption_at = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(self.last_caption_at))
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {speaker}: {text}\n")
        self._flush()

    def _flush(self) -> None:
        data = {key: getattr(self, attr) if attr else None for key, attr, _ in _STATUS_FIELDS}
        data.update(transcriptPath=str(self.transcript_path), pid=os.getpid())  # keeps table key order
        atomic_json_write(self.status_path, data)

    def set(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self._flush()


# JS injected into the Meet tab: MutationObserver on the caption container
# collects {speaker, text}; ``window.__hermesMeetDrain()`` pulls new entries.
_CAPTION_OBSERVER_JS = r"""
(() => {
  if (window.__hermesMeetInstalled) return;
  window.__hermesMeetInstalled = true;
  window.__hermesMeetQueue = [];

  const captionSelector = '[role="region"][aria-label*="aption" i], ' +
                          'div[jsname="YSxPC"], ' +  // legacy
                          'div[jsname="tgaKEf"]';    // current (Apr 2026)

  function pushEntry(speaker, text) {
    if (!text || !text.trim()) return;
    window.__hermesMeetQueue.push({
      ts: Date.now(),
      speaker: (speaker || '').trim(),
      text: text.trim(),
    });
  }

  function scan(root) {
    // Meet captions render as rows of speaker label + text block. Selectors
    // vary across Meet rewrites; try a few shapes and fall back to raw text.
    const rows = root.querySelectorAll('div[jsname="dsyhDe"], div.CNusmb, div.TBMuR');
    if (rows.length) {
      rows.forEach((row) => {
        const spkEl = row.querySelector('div.KcIKyf, div.zs7s8d, span[jsname="YSxPC"]');
        const txtEl = row.querySelector('div.bh44bd, span[jsname="tgaKEf"], div.iTTPOb');
        pushEntry(spkEl ? spkEl.innerText : '', txtEl ? txtEl.innerText : row.innerText);
      });
      return;
    }
    // Fallback: treat the whole region's innerText as one anonymous line.
    pushEntry('', (root.innerText || '').split('\n').filter(Boolean).pop());
  }

  function attach() {
    const el = document.querySelector(captionSelector);
    if (!el) return false;
    new MutationObserver(() => scan(el)).observe(el, { childList: true, subtree: true, characterData: true });
    scan(el);
    return true;
  }

  // Retry on interval — the caption region only appears after captions are
  // enabled and someone speaks.
  if (!attach()) {
    const iv = setInterval(() => { if (attach()) clearInterval(iv); }, 1500);
  }

  window.__hermesMeetDrain = () => {
    const out = window.__hermesMeetQueue.slice();
    window.__hermesMeetQueue = [];
    return out;
  };
})();
"""

# Best-effort caption toggle: Meet binds it to the ``c`` key; click targeting is too brittle.
_ENABLE_CAPTIONS_JS = (
    "(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', "
    "{ key: 'c', code: 'KeyC', keyCode: 67, which: 67, bubbles: true })); return true; })();")

_LEAVE_CALL_JS = (
    "() => { const b = document.querySelector('button[aria-label*=\"eave call\"]');"
    " if (b) b.click(); }")

# True once past the lobby: leave button, caption region (once our observer is installed)
# or participant list visible.
_ADMISSION_PROBE_JS = r"""
    (() => {
      if (document.querySelector('button[aria-label*="eave call" i]')) return true;
      if (window.__hermesMeetInstalled && document.querySelector(
          '[role="region"][aria-label*="aption" i], div[jsname="YSxPC"], div[jsname="tgaKEf"]')) return true;
      return !!document.querySelector('[aria-label*="articipants" i]');
    })();
    """

# English only — what Meet shows when the host denies or removes a guest.
_DENIED_PROBE_JS = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      return /You can't join this video call|You were removed from the meeting|No one responded to your request to join/i.test(text);
    })();
    """


def _probe(page, js: str) -> bool:
    """Evaluate a boolean JS probe; conservative — False on any error."""
    return bool(_quiet(page.evaluate, js))


def _visible(locator):
    """``locator.first`` if it exists and is visible, else None (swallows Playwright errors)."""
    return _quiet(lambda: locator.first if locator.first.count() and locator.first.is_visible() else None)


def _pcm_tail_loop(proc, pcm_path: Path, stop_flag: dict, poll_interval: float = 0.05) -> None:
    """Follow ``speaker.pcm`` as it grows and forward every appended chunk to the pump's stdin.
    The pump itself would hit EOF on the (empty) file at start-up and exit; this thread keeps
    feeding it until the stop flag is set or the pump dies."""
    try:
        with open(pcm_path, "rb") as f:
            while not stop_flag.get("stop") and proc.poll() is None:
                chunk = f.read(65536)
                if not chunk:
                    time.sleep(poll_interval)
                    continue
                proc.stdin.write(chunk)
                proc.stdin.flush()
    except (OSError, ValueError):
        pass  # pump exited / pipe closed (BrokenPipeError, write on closed stdin): nothing left to stream to
    finally:
        _quiet(proc.stdin.close)


def _start_pcm_pump(rt: dict, bridge_info: dict, pcm_path: Path, state: _BotState,
                    stop_flag: dict) -> None:
    """Stream the growing ``speaker.pcm`` (24kHz s16le mono) into the device Chrome's fake mic reads.
    The pump reads raw PCM from stdin (``-``) so audio appended after start-up is still played —
    pointed at the file it would read the empty sink to EOF and exit before Realtime spoke."""
    bridge_info = bridge_info or {}
    platform_tag = bridge_info.get("platform")
    target = bridge_info.get("write_target")
    if platform_tag == "linux":
        cmd = ["paplay", "--raw", "--rate=24000", "--format=s16le", "--channels=1",
               f"--device={target or 'hermes_meet_sink'}", "-"]
        missing = "paplay not found — install pulseaudio-utils for realtime on Linux"
    elif platform_tag == "darwin":
        # User must have BlackHole as default input; ffmpeg targets it by audiotoolbox index.
        if not shutil.which("ffmpeg"):
            state.set(error=_FFMPEG_MISSING)
            return
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-", "-f", "audiotoolbox",
               "-audio_device_index", _mac_audio_device_index(target or "BlackHole 2ch"), "-"]
        missing = _FFMPEG_MISSING
    else:
        return
    try:
        rt["pcm_pump"] = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        state.set(error=missing)
        return
    except Exception as e:
        if platform_tag != "darwin":
            raise
        state.set(error=f"macOS pcm pump failed to start: {e}")
        return
    rt["pcm_tail_thread"] = threading.Thread(
        target=_pcm_tail_loop, args=(rt["pcm_pump"], pcm_path, stop_flag), name="meet-pcm-tail", daemon=True)
    rt["pcm_tail_thread"].start()


def _start_realtime_speaker(rt: dict, cfg: _BotConfig, stop_flag: dict, state: _BotState) -> None:
    """Wire up the OpenAI Realtime session, the say-queue speaker thread and the PCM pump."""
    pcm_path, queue_path = cfg.out_dir / "speaker.pcm", cfg.out_dir / "say_queue.jsonl"
    pcm_path.write_bytes(b"")  # clean sink file per session
    queue_path.touch()  # so the speaker poller doesn't error on first iteration
    phase = "import"
    try:
        from .realtime.openai_client import RealtimeSession, RealtimeSpeaker
        phase = "connect"
        session = RealtimeSession(
            api_key=cfg.realtime_api_key, model=cfg.realtime_model, voice=cfg.realtime_voice,
            instructions=cfg.realtime_instructions, audio_sink_path=pcm_path, sample_rate=24000)
        session.connect()
    except Exception as e:
        state.set(error=f"realtime {phase} failed: {e}")
        return
    rt["session"] = session
    speaker = RealtimeSpeaker(session=session, queue_path=queue_path,
                              processed_path=cfg.out_dir / "say_processed.jsonl")

    def _speaker_loop():
        try:
            speaker.run_until_stopped(lambda: stop_flag.get("stop", False))
        except Exception as e:
            state.set(error=f"realtime speaker crashed: {e}")

    rt["speaker_thread"] = threading.Thread(target=_speaker_loop, name="meet-speaker", daemon=True)
    rt["speaker_thread"].start()
    _start_pcm_pump(rt, rt["bridge_info"], pcm_path, state, stop_flag)
    state.set(realtime_ready=True)


def _mac_audio_device_index(device_name: str) -> str:
    """ffmpeg ``-audio_device_index`` for *device_name* (case-insensitive; ``"0"`` if not found).
    ffmpeg prints the avfoundation device table on stderr as ``[N] Name``."""
    out = _quiet(subprocess.run, ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    needle = device_name.strip().lower()
    for line in (out.stderr if out else "").splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if m and m.group(2).strip().lower() == needle:
            return m.group(1)
    return "0"


def _setup_realtime(rt: dict, api_key: str, state: _BotState) -> None:
    """Provision the virtual audio bridge; on any failure fall back to transcribe mode."""
    if not api_key:
        state.set(error="realtime mode requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe")
        rt["enabled"] = False
        return
    try:
        from .audio_bridge import AudioBridge
        rt["bridge"] = AudioBridge()
        rt["bridge_info"] = rt["bridge"].setup()
        state.set(realtime=True, realtime_device=rt["bridge_info"].get("device_name"))
    except Exception as e:
        state.set(error=f"audio bridge setup failed: {e} — falling back to transcribe")
        rt["enabled"] = False


def _teardown_realtime(rt: dict) -> None:
    if rt.get("pcm_pump"):
        _quiet(rt["pcm_pump"].terminate)
        _quiet(rt["pcm_pump"].wait, timeout=3)
    for key, method, kw in (("pcm_tail_thread", "join", {"timeout": 1.0}),
                            ("speaker_thread", "join", {"timeout": 5.0}), ("session", "close", {}),
                            ("bridge", "teardown", {})):
        if rt.get(key) is not None:
            _quiet(getattr(rt[key], method), **kw)


_BotConfig = SimpleNamespace  # everything the bot reads from ``HERMES_MEET_*`` env vars


def _config_from_env() -> _BotConfig:
    env = os.environ.get
    out_raw = env("HERMES_MEET_OUT_DIR", "").strip()
    return _BotConfig(
        url=env("HERMES_MEET_URL", "").strip(),
        out_dir=Path(out_raw) if out_raw else None,
        headed=env("HERMES_MEET_HEADED", "").lower() in {"1", "true", "yes"},
        auth_state=env("HERMES_MEET_AUTH_STATE", "").strip(),
        guest_name=env("HERMES_MEET_GUEST_NAME", "Hermes Agent"),
        duration_s=_parse_duration(env("HERMES_MEET_DURATION", "")),
        realtime=env("HERMES_MEET_MODE", "transcribe").strip().lower() == "realtime",
        # HERMES_MEET_REALTIME_KEY is resolved by process_manager.start() via the parent's
        # profile secret scope; OPENAI_API_KEY only serves standalone `python -m` runs.
        realtime_api_key=env("HERMES_MEET_REALTIME_KEY") or env("OPENAI_API_KEY", ""),
        realtime_model=env("HERMES_MEET_REALTIME_MODEL", "gpt-realtime"),
        realtime_voice=env("HERMES_MEET_REALTIME_VOICE", "alloy"),
        realtime_instructions=env("HERMES_MEET_REALTIME_INSTRUCTIONS", ""),
        lobby_timeout=float(env("HERMES_MEET_LOBBY_TIMEOUT", "300")))


def _join(page, cfg: _BotConfig, state: _BotState, timeout: float = 30.0) -> None:
    """Fill the guest-name field and click 'Join now' / 'Ask to join' (the latter → lobby_waiting).
    Meet renders the pre-join buttons asynchronously after ``domcontentloaded``, so poll for up to
    *timeout* seconds instead of checking once — a single miss leaves the bot silently in the lobby."""
    deadline = time.time() + timeout
    while True:
        name_box = _visible(page.locator('input[aria-label*="name" i]'))
        if name_box is not None:
            _quiet(name_box.fill, cfg.guest_name, timeout=2_000)
        for label in ("Join now", "Ask to join"):
            btn = _visible(page.get_by_role("button", name=label, exact=False))
            if btn is not None and _quiet(lambda: (btn.click(timeout=3_000), True)):
                if label == "Ask to join":
                    state.set(lobby_waiting=True)
                return
        if time.time() >= deadline:
            return
        time.sleep(0.5)


def _ensure_mic_on(page) -> str:
    """Unmute the bot once admitted — Meet may seat an authenticated bot muted, and a muted mic makes
    realtime speech inaudible. The in-call toggle's aria-label is "Turn on microphone" while muted /
    "Turn off microphone" while live; returns ``unmuted_clicked`` / ``unmuted`` / ``unknown``
    (toggle not found: Meet variant changed or the label is localized)."""
    muted = _visible(page.locator('button[aria-label*="Turn on microphone" i]'))
    if muted is not None and _quiet(lambda: (muted.click(timeout=3_000), True)):
        return "unmuted_clicked"
    if _visible(page.locator('button[aria-label*="Turn off microphone" i]')) is not None:
        return "unmuted"
    return "unknown"


def _ensure_media_off(page) -> str:
    """LOCAL PATCH (Riz, 2026-11-23): listen-only bot turns its mic and camera off once admitted.
    Chrome's fake device sends a beep and a test video, so transcribe mode must not stay live.
    LOCAL PATCH 2026-11-24: click through the DOM (JS), because Meet's control tray defeats
    Playwright's actionability checks — the same fix that made the captions toggle work.
    Returns ``muted`` / ``unknown`` (mic toggle not found)."""
    detail = "{}"
    for attempt in range(3):
        with contextlib.suppress(Exception):
            detail = page.evaluate(_MEDIA_OFF_JS) or detail
        time.sleep(1.2)
        if _quiet(lambda: bool(page.evaluate(_MEDIA_STATE_JS))):
            break
    sys.stderr.write(f"google_meet bot: media off {detail}\n")
    if _visible(page.locator('button[aria-label*="Turn on microphone" i]')) is not None:
        return "muted"
    return "unknown"


# Click "Turn off camera" / "Turn off microphone" wherever Meet puts them (the main tray, the
# hover tray, or a More-options popover).
_MEDIA_OFF_JS = r"""
(() => {
  const btns = [...document.querySelectorAll('button,[role="button"]')];
  const lab = (b) => (b.getAttribute('aria-label') || '');
  const out = {};
  const hit = (key, re) => {
    const b = btns.find((x) => re.test(lab(x)));
    if (b) { b.click(); out[key] = 'clicked'; } else { out[key] = 'absent'; }
  };
  hit('camera', /turn off camera/i);
  hit('mic', /turn off microphone/i);
  return JSON.stringify(out);
})();
"""

# Both media toggles must read "Turn on …" once they are off.
_MEDIA_STATE_JS = r"""
(() => {
  const labels = [...document.querySelectorAll('button,[role="button"]')]
    .map((b) => b.getAttribute('aria-label') || '');
  const micOff = labels.some((l) => /turn on microphone/i.test(l));
  const camOff = labels.some((l) => /turn on camera/i.test(l));
  return micOff && camOff;
})();
"""


def _enable_captions_in_call(page, cfg=None) -> str:
    """LOCAL PATCH (Riz, 2026-11-24): turn captions on once admitted, via in-page JS clicks.
    Playwright's actionability checks fail on Meet's hover trays, so click the DOM directly.
    Newer Meet exposes a single "Captions and translation" button whose panel holds the
    captions switch; older UI has a direct "Turn on captions" button."""
    try:
        step1 = page.evaluate(_CAPTIONS_OPEN_JS)
    except Exception as e:
        sys.stderr.write(f"google_meet bot: caption step1 failed: {e}\n")
        return "error"
    if step1 in ("already_on", "clicked_direct"):
        return step1
    if step1 != "panel_opened":
        _quiet(lambda: (page.keyboard.press("c"), True))
        return f"key_c({step1})"
    time.sleep(1.8)
    try:
        detail = page.evaluate(_CAPTIONS_TOGGLE_JS)
    except Exception as e:
        detail = {"error": str(e)}
    time.sleep(2.0)
    state = "unknown"
    for _ in range(2):
        with contextlib.suppress(Exception):
            state = page.evaluate(_CAPTIONS_STATE_JS) or "unknown"
        if state in ("on", "off", "unknown"):
            break
        time.sleep(1.5)  # 'opened' → panel just opened, read it again
    if state == "opened":
        with contextlib.suppress(Exception):
            state = page.evaluate(_CAPTIONS_STATE_JS) or "unknown"
    if cfg is not None:
        with contextlib.suppress(Exception):
            (cfg.out_dir / "debug_captions_switch.json").write_text(
                json.dumps({"step1": step1, "detail": detail, "state": state}, indent=1, default=str))
        if state != "on":
            _quiet(lambda: _caption_debug_dump(page, cfg, "panel", "panel"))
            with contextlib.suppress(Exception):
                (cfg.out_dir / "debug_panel_dom.json").write_text(page.evaluate(_PANEL_DUMP_JS))
    _quiet(lambda: (page.keyboard.press("Escape"), True))
    if state == "on":
        return "panel_on"
    return "panel_toggle_failed"


_CAPTIONS_OPEN_JS = r"""
(() => {
  const btns = [...document.querySelectorAll('button,[role="button"]')];
  const label = (b) => (b.getAttribute('aria-label') || b.innerText || '');
  if (btns.some(b => /turn off captions/i.test(label(b)))) return 'already_on';
  const direct = btns.find(b => /turn on captions/i.test(label(b)));
  if (direct) { direct.click(); return 'clicked_direct'; }
  const panel = btns.find(b => /captions and translation/i.test(label(b)));
  if (!panel) return 'no_button';
  panel.click();
  return 'panel_opened';
})();
"""

_CAPTIONS_TOGGLE_JS = r"""(() => {
  const dlg = document.querySelector('[role="dialog"][aria-label*="aption" i]') ||
              document.querySelector('[role="dialog"]');
  if (!dlg) return {clicked: false, why: 'no_dialog'};
  const buttons = [...dlg.querySelectorAll('button,[role="button"]')];
  const lab = (b) => (b.getAttribute('aria-label') || b.innerText || '');
  if (buttons.some(b => /turn off captions/i.test(lab(b)))) return {clicked: false, already: true};
  const on = buttons.find(b => /turn on captions/i.test(lab(b)));
  if (!on) return {clicked: false, why: 'no_button', labels: buttons.map(lab).slice(0, 12)};
  on.click();
  return {clicked: true};
})();
"""

# After the click: re-open the panel (Meet may close it) and read the button label back.
_CAPTIONS_STATE_JS = r"""
(() => {
  const openPanel = () => [...document.querySelectorAll('button,[role="button"]')]
    .find(b => /captions and translation/i.test(b.getAttribute('aria-label') || ''));
  const read = () => {
    const dlg = document.querySelector('[role="dialog"][aria-label*="aption" i]');
    const t = dlg ? (dlg.innerText || '') : '';
    if (/turn off captions/i.test(t)) return 'on';
    if (/turn on captions/i.test(t)) return 'off';
    return null;
  };
  let s = read();
  if (s) return s;
  const p = openPanel();
  if (!p) return 'unknown';
  p.click();
  return 'opened';
})();
"""

_PANEL_DUMP_JS = r"""
(() => {
  const dlg = document.querySelector('[role="dialog"]');
  const pick = (e) => e ? {tag: e.tagName, role: e.getAttribute('role'),
      label: e.getAttribute('aria-label'), text: (e.innerText || '').slice(0, 1500),
      html: e.outerHTML.slice(0, 6000)} : null;
  return JSON.stringify({
    hasDialog: !!dlg,
    dialog: pick(dlg),
    switches: [...document.querySelectorAll('[role="switch"],[role="checkbox"],input[type="checkbox"]')]
      .map(pick),
    bodyText: (document.body.innerText || '').slice(0, 3000),
  }, null, 1);
})();
"""


# LOCAL PATCH (Riz, 2026-11-24): Google sign-in page instead of the Meet page = the saved
# sign-in has ended. Without this probe the bot waited silently until the duration ended.
_SIGNED_OUT_PROBE_JS = r"""
    (() => {
      if (location.hostname === 'accounts.google.com') return true;
      const text = document.body ? document.body.innerText || '' : '';
      return /Choose an account/i.test(text) && /Signed out/i.test(text);
    })();
    """

# LOCAL PATCH (Riz, 2026-11-24): auto-leave. A link join (an invite, not on the calendar) had no
# duration, so the bot stayed in the call after everyone left and blocked the watcher.
_CALL_ENDED_PROBE_JS = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      return /You left the meeting|call has ended|meeting has ended|been removed from the (meeting|call)|Return to home screen/i.test(text);
    })();
    """

# Participants in the call, the bot included. -1 = unknown (then the bot never leaves as "alone").
_PARTICIPANT_COUNT_JS = r"""
    (() => {
      const ids = new Set([...document.querySelectorAll('[data-participant-id]')]
        .map(e => e.getAttribute('data-participant-id')).filter(Boolean));
      const names = new Set();
      for (const e of document.querySelectorAll('[aria-label]')) {
        const m = /^More options for (.+)$/i.exec(e.getAttribute('aria-label') || '');
        if (m) names.add(m[1].trim());
      }
      const n = Math.max(ids.size, names.size);
      return n > 0 ? n : -1;
    })();
    """
_ALONE_LEAVE_S = 300.0       # alone this long after others were in the call -> leave
_NOBODY_LEAVE_S = 1200.0     # nobody else ever came (bot joins ~150 s early) -> leave
_DEFAULT_MAX_S = 3 * 3600.0  # limit for a join with no duration


def _profile_dir(cfg) -> Optional[Path]:
    """LOCAL PATCH (Riz, 2026-11-24): persistent Chromium profile for the signed-in bot.
    A fixed storage_state copy goes stale: Google rotates the sign-in cookies during each call,
    and a later run that sends the old cookies gets signed out. A persistent profile keeps the
    newest cookies. HERMES_MEET_PROFILE_DIR, else ``chrome-profile`` next to the auth state file.
    Used only when the directory exists (made by scripts/meet-auth-remote.sh)."""
    raw = os.environ.get("HERMES_MEET_PROFILE_DIR", "").strip()
    if raw:
        path = Path(raw)
    elif cfg.auth_state:
        path = Path(cfg.auth_state).parent / "chrome-profile"
    else:
        return None
    return path if path.is_dir() else None


def _die_with_parent() -> None:
    """LOCAL PATCH: Xvfb gets SIGTERM when the bot dies, also after a SIGKILL (Linux prctl)."""
    with contextlib.suppress(Exception):
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG

def _start_own_xvfb() -> Optional[subprocess.Popen]:
    """LOCAL PATCH (Riz, 2026-11-24): virtual screen for a headed bot on a server with no screen.
    Picks a free display from :140, sets DISPLAY for Chromium, returns the Xvfb process (or None
    when Xvfb is missing or does not start; the caller then runs headless)."""
    if not shutil.which("Xvfb"):
        return None
    for num in range(140, 200):
        if Path(f"/tmp/.X11-unix/X{num}").exists() or Path(f"/tmp/.X{num}-lock").exists():
            continue
        proc = _quiet(subprocess.Popen, ["Xvfb", f":{num}", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      preexec_fn=_die_with_parent)
        if proc is None:
            return None
        for _ in range(30):
            if Path(f"/tmp/.X11-unix/X{num}").exists():
                os.environ["DISPLAY"] = f":{num}"
                atexit.register(lambda: (_quiet(proc.terminate), _quiet(proc.wait, timeout=5)))
                return proc
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        _quiet(proc.kill)
    return None


# LOCAL PATCH (Riz, 2026-11-24): record the call audio and transcribe it after the call with
# Groq Whisper (meet_transcribe.py). Live captions were too poor ("but", "but J", "but Jr",
# speaker "Unknown"). Each bot gets its own PulseAudio null sink; Chromium plays into it
# (PULSE_SINK), ffmpeg records its monitor. Nothing is audible anywhere. HERMES_MEET_RECORD=0 = off.
def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / "tools/hermes/hermes-home"))


def _ffmpeg_binary() -> Optional[str]:
    explicit = os.environ.get("HERMES_MEET_FFMPEG", "").strip()
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = sorted(_hermes_home().glob("tools/ffmpeg-*/bin/ffmpeg"))
    return str(bundled[-1]) if bundled else None


def _pactl(*args: str) -> Optional[str]:
    done = _quiet(subprocess.run, ["pactl", *args], capture_output=True, text=True, timeout=10)
    return done.stdout.strip() if done is not None and done.returncode == 0 else None


def _unload_stale_sinks() -> None:
    """Remove null sinks of bots that died without cleanup (SIGKILL)."""
    for line in (_pactl("list", "short", "modules") or "").splitlines():
        match = re.search(r"^(\d+)\s+module-null-sink\s+.*sink_name=hermes_meet_(\d+)", line)
        if match and not Path(f"/proc/{match.group(2)}").exists():
            _pactl("unload-module", match.group(1))


def _start_recording(cfg) -> Optional[dict]:
    if os.environ.get("HERMES_MEET_RECORD", "1").lower() in {"0", "false", "no"}:
        return None
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg or not shutil.which("pactl"):
        sys.stderr.write("google_meet bot: no recording (ffmpeg or pactl missing)\n")
        return None
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    _unload_stale_sinks()
    sink = f"hermes_meet_{os.getpid()}"
    module = _pactl("load-module", "module-null-sink", f"sink_name={sink}",
                    f"sink_properties=device.description={sink}")
    if not module:
        sys.stderr.write("google_meet bot: no recording (PulseAudio sink failed)\n")
        return None
    audio = cfg.out_dir / "audio.ogg"
    for stale in ("audio.ogg", "captions.txt", "transcribe.json", "recording.json"):  # earlier run
        with contextlib.suppress(OSError):
            (cfg.out_dir / stale).unlink()
    log = open(cfg.out_dir / "ffmpeg.log", "w", encoding="utf-8")
    proc = _quiet(subprocess.Popen,
                  [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-f", "pulse", "-i", f"{sink}.monitor",
                   "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(audio)],
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, preexec_fn=_die_with_parent)
    if proc is None:
        _pactl("unload-module", module)
        sys.stderr.write("google_meet bot: no recording (ffmpeg did not start)\n")
        return None
    os.environ["PULSE_SINK"] = sink  # Chromium inherits it
    (cfg.out_dir / "recording.json").write_text(json.dumps({"started_at": time.time(), "sink": sink}))
    rec = {"proc": proc, "module": module, "audio": audio, "stopped": False}
    atexit.register(_stop_recording, rec)
    threading.Thread(target=_keep_sink_unmuted, args=(rec, sink), daemon=True).start()
    sys.stderr.write(f"google_meet bot: recording audio to {audio}\n")
    return rec


def _keep_sink_unmuted(rec: dict, sink: str) -> None:
    """PulseAudio restores a saved mute for every "Chromium" stream (module-stream-restore).
    Unmute only the streams that play into this bot's own sink; nothing else is touched."""
    while not rec["stopped"]:
        sinks = _quiet(json.loads, _pactl("-f", "json", "list", "short", "sinks") or "[]") or []
        sink_ids = {s.get("index") for s in sinks if s.get("name") == sink}
        inputs = _quiet(json.loads, _pactl("-f", "json", "list", "sink-inputs") or "[]") or []
        for item in inputs:
            if item.get("sink") in sink_ids and item.get("mute"):
                _pactl("set-sink-input-mute", str(item.get("index")), "0")
        time.sleep(2.0)


def _stop_recording(rec: Optional[dict]) -> None:
    if not rec or rec["stopped"]:
        return
    rec["stopped"] = True
    proc = rec["proc"]
    _quiet(proc.send_signal, signal.SIGINT)  # ffmpeg closes the file cleanly on SIGINT
    if _quiet(proc.wait, timeout=5) is None and proc.poll() is None:
        _quiet(proc.kill)
    _pactl("unload-module", rec["module"])


def _spawn_transcriber(cfg, rec: Optional[dict]) -> None:
    """Start meet_transcribe.py detached, so it survives the process manager's SIGKILL."""
    if not rec or not rec["audio"].is_file() or rec["audio"].stat().st_size < 10_000:
        return
    status = cfg.out_dir / "transcribe.json"
    status.write_text(json.dumps({"status": "pending", "at": time.time()}))
    script = Path(__file__).with_name("meet_transcribe.py")
    log = open(cfg.out_dir / "transcribe.log", "a", encoding="utf-8")
    _quiet(subprocess.Popen, [sys.executable, "-I", str(script), str(cfg.out_dir)],
           stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
           env={**os.environ, "HERMES_HOME": str(_hermes_home())})
    sys.stderr.write("google_meet bot: audio transcription started\n")


def _real_user_agent(executable: Optional[str]) -> str:
    """LOCAL PATCH (Riz, 2026-11-24): send the real Chrome version, not a fixed Chrome/124.
    Meet shows "This browser version is no longer supported" for an old version."""
    fallback = _CONTEXT_ARGS["user_agent"]
    if not executable:
        return fallback
    out = _quiet(subprocess.run, [executable, "--version"], capture_output=True, text=True, timeout=10)
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", getattr(out, "stdout", "") or "")
    if not match:
        return fallback
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{match.group(1)}.0.0.0 Safari/537.36")


_CAPTION_DEBUG_JS = r"""
    (() => {
      const pick = document.querySelectorAll('[role="region"], [aria-live], div[jsname="tgaKEf"], div[jsname="YSxPC"], div[jsname="dsyhDe"]');
      const out = [];
      pick.forEach((el) => out.push({tag: el.tagName, role: el.getAttribute('role'), label: el.getAttribute('aria-label'),
        live: el.getAttribute('aria-live'), jsname: el.getAttribute('jsname'), cls: el.className,
        text: (el.innerText || '').slice(0, 300), html: el.outerHTML.slice(0, 1500)}));
      const btns = [...document.querySelectorAll('button[aria-label*="aption" i]')].map(b => b.getAttribute('aria-label'));
      return {captionButtons: btns, nodes: out.slice(0, 25)};
    })();
    """


def _caption_debug_dump(page, cfg, how: str, tag: str = "incall") -> None:
    """LOCAL PATCH debug aid: one screenshot and caption DOM sample per call, ~20 s after join."""
    if not cfg or not cfg.out_dir:
        return
    _quiet(page.screenshot, path=str(cfg.out_dir / f"debug_{tag}.png"))
    sample = _quiet(page.evaluate, _CAPTION_DEBUG_JS)
    with contextlib.suppress(Exception):
        (cfg.out_dir / f"debug_captions_{tag}.json").write_text(
            json.dumps({"enable": how, "sample": sample}, indent=1), encoding="utf-8")


def _drain_loop(page, cfg: _BotConfig, state: _BotState, rt: dict, stop_flag: dict) -> None:
    """Admission + caption drain loop until SIGTERM, duration expiry, lobby timeout/denial or page loss.
    Sets ``leave_reason`` for every exit but SIGTERM; triggers barge-in; mirrors realtime counters."""
    # LOCAL PATCH: a join with no duration gets a 3 h limit (auto-leave safety net).
    deadline = time.time() + (cfg.duration_s or _DEFAULT_MAX_S)
    lobby_deadline = time.time() + cfg.lobby_timeout
    last_admission_check = 0.0
    captions = {"how": None, "dump_at": None}  # LOCAL PATCH
    presence = {"checked": 0.0, "alone_since": None, "seen_others": False}  # LOCAL PATCH auto-leave
    while not stop_flag["stop"]:
        now = time.time()
        if deadline and now > deadline:
            state.set(leave_reason="duration_expired")
            return
        if state.in_call and (now - presence["checked"]) > 10.0:  # LOCAL PATCH auto-leave
            presence["checked"] = now
            if _probe(page, _CALL_ENDED_PROBE_JS):
                state.set(leave_reason="meeting_ended")
                sys.stderr.write("google_meet bot: call ended, leaving\n")
                return
            count = _quiet(page.evaluate, _PARTICIPANT_COUNT_JS)
            if isinstance(count, (int, float)) and count >= 2:
                presence["seen_others"] = True
            if count == 1:
                presence["alone_since"] = presence["alone_since"] or now
                limit = _ALONE_LEAVE_S if presence["seen_others"] else _NOBODY_LEAVE_S
                if now - presence["alone_since"] >= limit:
                    state.set(leave_reason="alone")
                    sys.stderr.write(f"google_meet bot: alone for {int(limit)}s, leaving\n")
                    return
            else:
                presence["alone_since"] = None
        if not state.in_call and (now - last_admission_check) > 3.0:
            last_admission_check = now
            if _probe(page, _ADMISSION_PROBE_JS):
                media = _ensure_mic_on(page) if rt["enabled"] else _ensure_media_off(page)  # LOCAL PATCH
                state.set(in_call=True, lobby_waiting=False, joined_at=now, mic_state=media)
                captions["how"] = _enable_captions_in_call(page, cfg)  # LOCAL PATCH
                captions["dump_at"] = now + 20
                sys.stderr.write(f"google_meet bot: in call, mic={media}, captions={captions['how']}\n")
            elif now > lobby_deadline:
                waited = int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0
                state.set(error=f"lobby timeout — host never admitted the bot within {waited}s",
                          leave_reason="lobby_timeout")
                return
            elif _probe(page, _SIGNED_OUT_PROBE_JS):  # LOCAL PATCH (2026-11-24)
                state.set(error="google sign-in ended (signed out) — run scripts/meet-auth-remote.sh",
                          leave_reason="signed_out")
                sys.stderr.write("google_meet bot: signed out of Google — sign in again with "
                                 "scripts/meet-auth-remote.sh\n")
                return
            elif _probe(page, _DENIED_PROBE_JS):
                state.set(error="host denied admission", leave_reason="denied")
                return
        if captions["dump_at"] and now >= captions["dump_at"]:  # LOCAL PATCH debug aid
            captions["dump_at"] = None
            _caption_debug_dump(page, cfg, captions["how"])
        try:
            queued = page.evaluate("window.__hermesMeetDrain && window.__hermesMeetDrain()")
            for entry in (e for e in (queued if isinstance(queued, list) else ()) if isinstance(e, dict)):
                speaker = str(entry.get("speaker", ""))
                state.record_caption(speaker=speaker, text=str(entry.get("text", "")))
                # Barge-in: a real human spoke while we may be generating audio.
                if (rt["session"] is not None and _looks_like_human_speaker(speaker, cfg.guest_name)
                        and _quiet(rt["session"].cancel_response)):
                    state.set(last_barge_in_at=now)
        except Exception:
            if page.is_closed():  # Meet reloaded or we got booted — exit rather than spin
                state.set(leave_reason="page_closed")
                return
        if rt["session"] is not None:
            state.set(audio_bytes_out=rt["session"].audio_bytes_out,
                      last_audio_out_at=rt["session"].last_audio_out_at)
        time.sleep(1.0)


_CONTEXT_ARGS = {
    "viewport": {"width": 1280, "height": 800},
    "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "permissions": ["microphone", "camera"]}


def run_bot() -> int:
    cfg = _config_from_env()
    if not _is_safe_meet_url(cfg.url):
        sys.stderr.write("google_meet bot: refusing to launch — HERMES_MEET_URL must be a "
                         f"meet.google.com URL. got: {cfg.url!r}\n")
        return 2
    if cfg.out_dir is None:
        sys.stderr.write("google_meet bot: HERMES_MEET_OUT_DIR is required\n")
        return 2
    state = _BotState(out_dir=cfg.out_dir, meeting_id=_meeting_id_from_url(cfg.url), url=cfg.url)
    # SIGTERM sets a flag (not an exception) so the Playwright teardown below still runs
    # and ``meet_leave`` gets a finalized transcript.
    stop_flag = {"stop": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop_flag.__setitem__("stop", True))
    # Realtime resources in one dict so teardown works however we exit.
    rt = dict(enabled=cfg.realtime, bridge=None, bridge_info=None, session=None, speaker_thread=None)
    if rt["enabled"]:
        _setup_realtime(rt, cfg.realtime_api_key, state)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state.set(error=f"playwright not installed: {e}", exited=True)
        sys.stderr.write("google_meet bot: playwright is not installed. Run "
                         "`hermes meet install`\n")
        if rt["bridge"]:
            rt["bridge"].teardown()
        return 3
    chrome_args = ["--use-fake-ui-for-media-stream", "--disable-blink-features=AutomationControlled",
                   "--mute-audio"]  # LOCAL PATCH 2026-11-24: bot must not play call audio
    if not rt["enabled"]:
        chrome_args.insert(1, "--use-fake-device-for-media-stream")  # silent fake mic
    elif rt["bridge_info"] and rt["bridge_info"].get("platform") == "linux":
        # Playwright's launch() takes no env: set PULSE_SOURCE on ourselves so Chrome inherits it.
        os.environ["PULSE_SOURCE"] = rt["bridge_info"].get("device_name", "")
    context_args = dict(_CONTEXT_ARGS)
    executable = chromium_executable()
    context_args["user_agent"] = _real_user_agent(executable)  # LOCAL PATCH (2026-11-24)
    profile = _profile_dir(cfg)  # LOCAL PATCH (2026-11-24)
    if profile is None and cfg.auth_state and Path(cfg.auth_state).is_file():
        context_args["storage_state"] = cfg.auth_state
    # LOCAL PATCH (2026-11-24): headed Chromium in a virtual screen when the profile is used.
    # Measured on this server: less CPU and memory than headless, and it matches the sign-in browser.
    headed = cfg.headed or (profile is not None and
                            os.environ.get("HERMES_MEET_HEADLESS", "").lower() not in {"1", "true", "yes"})
    if headed and not os.environ.get("DISPLAY") and _start_own_xvfb() is None:
        headed = False
    sys.stderr.write(f"google_meet bot: {'headed on ' + os.environ.get('DISPLAY', '?') if headed else 'headless'}\n")
    # LOCAL PATCH (2026-11-24): audio recording (transcribe mode only); Chromium must play audio.
    rec = None if rt["enabled"] else _start_recording(cfg)
    if rec is not None and "--mute-audio" in chrome_args:
        chrome_args.remove("--mute-audio")
    # Playwright adds --mute-audio itself in headless mode.
    unmute = {"ignore_default_args": ["--mute-audio"]} if rec is not None else {}
    sys.stderr.write(f"google_meet bot: sign-in from {'profile ' + str(profile) if profile else context_args.get('storage_state', 'none (guest)')}, "
                     f"ua={context_args['user_agent'].split(') ')[-1]}\n")
    try:
        with sync_playwright() as pw:
            if profile is not None:  # LOCAL PATCH (2026-11-24)
                browser = None
                context = pw.chromium.launch_persistent_context(
                    str(profile), channel="chromium", executable_path=executable,
                    headless=not headed, args=chrome_args, **unmute, **context_args)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = pw.chromium.launch(
                    channel="chromium", executable_path=executable,
                    headless=not headed, args=chrome_args, **unmute,
                )
                context = browser.new_context(**context_args)
                page = context.new_page()
            try:
                page.goto(cfg.url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                state.set(error=f"navigate failed: {e}", exited=True)
                return 4
            _join(page, cfg, state)
            if _quiet(page.evaluate, _ENABLE_CAPTIONS_JS):
                state.set(captions_enabled_attempted=True)
            try:
                page.evaluate(_CAPTION_OBSERVER_JS)
            except Exception as e:
                state.set(error=f"caption observer install failed: {e}")
            # in_call stays False until admission is confirmed by the drain loop.
            state.set(captioning=True, join_attempted_at=time.time())
            if rt["enabled"]:
                _start_realtime_speaker(rt, cfg, stop_flag, state)
            _drain_loop(page, cfg, state, rt, stop_flag)
            _quiet(page.evaluate, _LEAVE_CALL_JS)
            context.close()
            if browser is not None:  # LOCAL PATCH (2026-11-24): persistent profile has no browser
                browser.close()
            _teardown_realtime(rt)
            state.set(in_call=False, captioning=False, exited=True)
            return 0
    except Exception as e:
        state.set(error=f"unhandled: {e}", exited=True)
        return 1
    finally:  # LOCAL PATCH (2026-11-24): close the recording, then transcribe it detached
        _stop_recording(rec)
        _spawn_transcriber(cfg, rec)


def _looks_like_human_speaker(speaker: str, bot_guest_name: str) -> bool:
    """Whether a caption's speaker is probably a human rather than our own echo (Meet attributes
    our fake-mic audio to the bot's name; blank/unknown speakers are ambiguous — no barge-in)."""
    return bool(speaker and speaker.strip()) and (
        speaker.strip().lower() not in {"unknown", "you", bot_guest_name.strip().lower()})


_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def _parse_duration(raw: str) -> Optional[float]:
    """Parse ``30m`` / ``2h`` / ``90`` (seconds) → float seconds, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    mult = _DURATION_UNITS.get(raw[-1:])
    try:
        return float(raw[:-1]) * mult if mult else float(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover — subprocess entry point
    sys.exit(run_bot())
