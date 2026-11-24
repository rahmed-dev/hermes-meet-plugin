#!/usr/bin/env python3
"""Auto-join Google Meet events from Riz's calendar, read through the Google bot account.

Run every 2 minutes by the Hermes cron scheduler in no-agent mode:

    hermes cron create "*/2 * * * *" --script meet_calendar_watch.py --no-agent \
        --name meet-calendar-watch --deliver discord:<chat_id>

What it does, in order:
  1. read Riz's calendar (MEET_CALENDAR_ID in $HERMES_HOME/.env, shared with the bot account)
     through the Google Calendar API with the google-workspace token (google_token.json)
  2. keep events that have a meet.google.com link and are not cancelled / all-day / declined
  3. join (via plugins.google_meet process_manager) events starting within JOIN_LEAD seconds,
     once per (uid, start) pair, auto-leaving TAIL_MINUTES after the event's end
  4. when a joined event is over: archive transcript.txt next to it and queue a one-shot
     Hermes cron job that summarises the call into Discord

Quiet by design: prints nothing when there is nothing to do, so a no-agent cron job only
posts to Discord when it actually joins or finishes a meeting.

Usage:
    python meet_calendar_watch.py            # normal run (what cron does)
    python meet_calendar_watch.py --dry-run  # show decisions, join nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "/home/riz/tools/hermes/hermes-home")
WATCHER_DIR = HERMES_HOME / "workspace" / "meetings" / "watcher"
STATE_PATH = WATCHER_DIR / "state.json"
LOG_PATH = WATCHER_DIR / "watcher.log"

# Load the google_meet plugin from the Hermes source tree, where the local patches are (mute in
# listen-only mode, captions after admission). Cron scripts otherwise import the frozen copy in
# installs/*/workspace/. PYTHONPATH carries the same choice into the bot subprocess.
AGENT_TREE = str(HERMES_HOME / "hermes-agent")
sys.path.insert(0, AGENT_TREE)
os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (AGENT_TREE, os.environ.get("PYTHONPATH", "")) if p)
# LOCAL (2026-11-24): our patched Meet plugin is a user override plugin in $HERMES_HOME/plugins,
# imported as package "google_meet"; the Hermes source tree stays unmodified.
USER_PLUGINS = str(HERMES_HOME / "plugins")
sys.path.insert(0, USER_PLUGINS)
GOOGLE_TOKEN = HERMES_HOME / "google_token.json"
AUTH_JSON = HERMES_HOME / "workspace" / "meetings" / "auth.json"

JOIN_LEAD_SECONDS = int(os.environ.get("MEET_WATCH_JOIN_LEAD", "150"))
LATE_JOIN_GRACE_SECONDS = int(os.environ.get("MEET_WATCH_LATE_GRACE", "120"))
TAIL_MINUTES = int(os.environ.get("MEET_WATCH_TAIL_MINUTES", "5"))
GUEST_NAME = os.environ.get("MEET_WATCH_GUEST_NAME", "Hermes Agent")
HERMES_BIN = os.environ.get("MEET_WATCH_HERMES_BIN", "/home/riz/.local/bin/hermes")

MEET_RE = re.compile(r"https://meet\.google\.com/[a-z0-9]{3}-[a-z0-9]{4}-[a-z0-9]{3}")


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    try:
        WATCHER_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meetings": {}}


def write_state(state: dict) -> None:
    WATCHER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def env_value(key: str) -> str:
    """Env first, then the profile .env (cron subprocesses may not carry the whole profile env)."""
    value = os.environ.get(key, "").strip()
    if value:
        return value
    for candidate in (HERMES_HOME / ".env", Path.home() / ".hermes" / ".env"):
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def deliver_target() -> str:
    """Discord home channel for summary posts."""
    explicit = env_value("MEET_WATCH_DELIVER")
    if explicit:
        return explicit
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load((HERMES_HOME / "config.yaml").read_text(encoding="utf-8")) or {}
        home = ((cfg.get("platforms") or {}).get("discord") or {}).get("home_channel") or {}
        chat_id = str(home.get("chat_id") or "").strip()
        if chat_id:
            return f"discord:{chat_id}"
    except Exception:
        pass
    return "discord"


def fetch_events(calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Events of the shared calendar in the window, recurrences expanded by Google."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN))
    if not creds.valid:
        creds.refresh(Request())
        # keep the refreshed access token; the refresh token itself does not change
        tmp = GOOGLE_TOKEN.with_suffix(".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, GOOGLE_TOKEN)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    items, page = [], None
    while True:
        resp = service.events().list(
            calendarId=calendar_id, timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
            singleEvents=True, orderBy="startTime", showDeleted=False, pageToken=page,
        ).execute()
        items += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return items


def as_aware(value: dict) -> datetime | None:
    """API start/end -> aware datetime (all-day events have only 'date' and return None)."""
    raw = (value or {}).get("dateTime")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def event_meet_url(event: dict) -> str:
    entry_points = (event.get("conferenceData") or {}).get("entryPoints") or []
    haystack = " ".join([event.get("hangoutLink", ""), *(e.get("uri", "") for e in entry_points),
                         event.get("description", ""), event.get("location", "")])
    match = MEET_RE.search(haystack)
    return match.group(0) if match else ""


def is_declined(event: dict, calendar_id: str) -> bool:
    for attendee in event.get("attendees") or []:
        if str(attendee.get("email", "")).lower() == calendar_id.lower():
            return attendee.get("responseStatus") == "declined"
    return False


def is_cancelled(event: dict) -> bool:
    return event.get("status") == "cancelled"


def duration_arg(seconds: int) -> str:
    return f"{max(60, int(seconds))}s"


# --------------------------------------------------------------------------- meeting engine

def process_manager():
    from google_meet import process_manager as pm  # noqa: PLC0415 — user override plugin
    return pm


def queue_summary(meeting: dict, transcript_path: Path, lines: int) -> str | None:
    """Create a one-shot cron job that reads the transcript and posts the recap to Discord."""
    deliver = deliver_target()
    prompt = (
        "A calendar-watched Google Meet call has just ended. "
        f"Meeting: {meeting.get('summary', '(untitled)')} — {meeting.get('start_h')} to {meeting.get('end_h')} PKT. "
        f"URL: {meeting.get('url')}. Transcript file: {transcript_path} ({lines} caption lines).\n\n"
        "Read the transcript file, then post a compact recap to Discord with: 3-6 bullet summary, "
        "decisions made, action items with owners and dates (write 'owner not stated' when the "
        "transcript does not name one). If the transcript is too thin to summarise honestly, say so "
        "instead of inventing content. Do not claim anything the transcript does not say."
    )
    name = f"meet-recap-{meeting.get('meeting_id', 'meeting')}"
    cmd = [HERMES_BIN, "cron", "create", "1m", prompt, "--name", name, "--repeat", "1",
           "--deliver", deliver, "--no-wrap-response"]
    env = {**os.environ, "HERMES_HOME": str(HERMES_HOME)}
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception as exc:
        log(f"summary job spawn failed: {exc}")
        return None
    if done.returncode != 0:
        log(f"summary job spawn exit {done.returncode}: {(done.stderr or done.stdout).strip()[:400]}")
        return None
    log(f"queued recap job {name} -> {deliver}")
    return name


def transcription_pending(meeting: dict) -> bool:
    """LOCAL PATCH: audio recorded, Whisper transcript not finished (or not yet started)."""
    out_dir = Path(meeting.get("out_dir") or "")
    if not (out_dir / "audio.ogg").is_file():
        return False
    try:
        state = json.loads((out_dir / "transcribe.json").read_text()).get("status")
    except (OSError, ValueError):
        state = None
    return state not in {"done", "failed", "skipped"}


def archive_transcript(meeting: dict) -> Path | None:
    out_dir = Path(meeting.get("out_dir") or "")
    src = out_dir / "transcript.txt"
    if not src.is_file() or src.stat().st_size == 0:
        return None
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M")
    dest = out_dir / f"transcript-{stamp}.txt"
    try:
        dest.write_bytes(src.read_bytes())
    except OSError as exc:
        log(f"archive failed for {src}: {exc}")
        return None
    return dest


def run(dry_run: bool) -> list[str]:
    notes: list[str] = []
    fake_now = os.environ.get("MEET_WATCH_FAKE_NOW", "").strip()
    now = datetime.fromisoformat(fake_now) if fake_now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    state = read_state()
    meetings = state.setdefault("meetings", {})

    calendar_id = env_value("MEET_CALENDAR_ID")
    if not calendar_id:
        log("MEET_CALENDAR_ID missing — nothing to do")
        return notes

    window_start = now - timedelta(minutes=30)
    window_end = now + timedelta(minutes=45)
    try:
        events = fetch_events(calendar_id, window_start, window_end)
        state["last_fetch"] = now.isoformat()
        state.pop("last_error", None)
    except Exception as exc:
        state["last_error"] = f"calendar fetch failed: {exc}"
        write_state(state)
        log(f"calendar fetch failed: {exc}")
        return notes

    pm = None if dry_run else process_manager()
    active = {}
    if pm is not None:
        try:
            status = pm.status()
            active = status if status.get("ok") and status.get("alive") else {}
        except Exception as exc:
            log(f"status check failed: {exc}")

    candidates = []
    for event in events:
        if is_cancelled(event) or is_declined(event, calendar_id):
            continue
        start = as_aware(event.get("start"))
        end = as_aware(event.get("end"))
        if start is None or end is None:
            continue  # all-day
        url = event_meet_url(event)
        if not url:
            continue
        uid = str(event.get("iCalUID") or event.get("id", ""))
        key = f"{uid}|{start.isoformat()}"
        candidates.append(dict(key=key, event=event, start=start, end=end, url=url,
                               summary=str(event.get("summary") or "(untitled)")))

    # --- finish anything that is over -------------------------------------
    for key, meeting in list(meetings.items()):
        if meeting.get("state") != "joined":
            continue
        end = datetime.fromisoformat(meeting["end"])
        if now < end + timedelta(minutes=TAIL_MINUTES):
            continue
        label = f"{meeting.get('summary', '(untitled)')} ({meeting.get('meeting_id')})"
        if dry_run:
            notes.append(f"would finish: {label}")
        else:
            if pm is not None:
                try:
                    status = pm.status()
                    if status.get("ok") and status.get("alive") and status.get("meetingId") == meeting.get("meeting_id"):
                        pm.stop(reason="calendar event ended")
                except Exception as exc:
                    log(f"stop failed for {label}: {exc}")
            # LOCAL PATCH (Riz, 2026-11-24): the bot records audio and Groq Whisper rewrites
            # transcript.txt after the call. Wait for that (up to 20 min) before the recap.
            if transcription_pending(meeting) and now < end + timedelta(minutes=TAIL_MINUTES + 20):
                if not meeting.get("waiting_transcription"):
                    meeting["waiting_transcription"] = now.isoformat()
                    log(f"waiting for audio transcription: {label}")
                continue
            archived = archive_transcript(meeting)
            transcript = Path(meeting.get("out_dir") or "") / "transcript.txt"
            lines = 0
            if transcript.is_file():
                lines = len([ln for ln in transcript.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()])
            if lines:
                job = queue_summary(meeting, archived or transcript, lines)
                notes.append(f"finished: {label} — {lines} caption lines"
                             + (f", recap queued ({job})" if job else ", recap queue FAILED"))
            else:
                notes.append(f"finished: {label} — no captions captured, nothing to summarise")
            meeting["state"] = "done"
            meeting["finished_at"] = now.isoformat()
            meeting["caption_lines"] = lines

    # --- join anything starting now ---------------------------------------
    for cand in candidates:
        start, end, url, key = cand["start"], cand["end"], cand["url"], cand["key"]
        if key in meetings and meetings[key].get("state") in {"joined", "done"}:
            continue
        seconds_to_start = (start - now).total_seconds()
        seconds_since_start = -seconds_to_start
        if seconds_to_start > JOIN_LEAD_SECONDS or seconds_since_start > LATE_JOIN_GRACE_SECONDS:
            continue
        if now >= end:
            continue
        label = f"{cand['summary']} ({url})"
        if active and active.get("meetingId") != url:
            notes.append(f"skipped {label}: another meeting bot is already in a call")
            continue
        import hashlib
        from urllib.parse import urlparse
        meeting_id = hashlib.sha1(urlparse(url).path.encode()).hexdigest()[:8]
        stay = int((end - now).total_seconds()) + TAIL_MINUTES * 60
        if dry_run:
            notes.append(f"would join: {label} at {start.astimezone().strftime('%H:%M')}, stay {duration_arg(stay)}")
            continue
        # keep the previous run's transcript before process_manager wipes the folder
        previous = meetings.get(key) or {}
        old_out = Path(previous.get("out_dir") or (HERMES_HOME / "workspace" / "meetings" / meeting_id))
        if (old_out / "transcript.txt").is_file():
            stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M")
            try:
                (old_out / f"transcript-{stamp}.txt").write_bytes((old_out / "transcript.txt").read_bytes())
            except OSError:
                pass
        try:
            res = pm.start(url, guest_name=GUEST_NAME, duration=duration_arg(stay),
                           auth_state=str(AUTH_JSON) if AUTH_JSON.is_file() else None,
                           session_id="calendar-watcher", mode="transcribe")
        except Exception as exc:
            log(f"join failed for {label}: {exc}")
            notes.append(f"join FAILED for {label}: {exc}")
            continue
        if not res.get("ok"):
            notes.append(f"join refused for {label}: {res.get('error') or res.get('reason')}")
            continue
        meetings[key] = {
            "state": "joined", "url": url, "meeting_id": res.get("meeting_id") or meeting_id,
            "out_dir": res.get("out_dir"), "pid": res.get("pid"),
            "start": start.isoformat(), "end": end.isoformat(),
            "summary": cand["summary"], "joined_at": now.isoformat(),
            "start_h": start.astimezone().strftime("%H:%M"),
            "end_h": end.astimezone().strftime("%H:%M"),
        }
        notes.append(f"joined: {cand['summary']} at {start.astimezone().strftime('%H:%M')} — "
                     f"staying {duration_arg(stay)}, transcript in {res.get('out_dir')}")

    # keep state bounded
    for key in [k for k, v in meetings.items() if v.get("state") == "done"]:
        finished = v_end = None
        try:
            finished = datetime.fromisoformat(meetings[key].get("finished_at", ""))
        except ValueError:
            finished = None
        if finished and (now - finished).days > 14:
            meetings.pop(key, None)

    state["last_run"] = now.isoformat()
    write_state(state)
    return notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        notes = run(args.dry_run)
    except Exception:
        log("crash:\n" + traceback.format_exc())
        raise
    if notes:
        print("\n".join(notes))
        log(" | ".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
