"""LOCAL PATCH (Riz, 2026-11-24): transcribe a recorded Meet call with Groq Whisper.

Started detached by meet_bot.py after the call:  python -I meet_transcribe.py <meeting out_dir>

Reads   audio.ogg, recording.json (start time), transcript.txt (live captions)
Writes  captions.txt   - the live caption lines (moved from transcript.txt)
        transcript.txt - the Whisper text, "[HH:MM:SS] Speaker: text"; the speaker name comes
                         from the caption lines at the same time, where they name one
        transcribe.json - {"status": "done" | "failed" | "skipped", ...}
On failure transcript.txt keeps the caption lines, so the recap still has something.

Settings (environment or $HERMES_HOME/.env): GROQ_API_KEY (required),
MEET_STT_MODEL (default whisper-large-v3-turbo), MEET_STT_LANGUAGE (default: auto-detect).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CHUNK_SECONDS = 3600  # 1 h of 24 kbit/s opus is about 11 MB, under Groq's 25 MB limit
CAPTION_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] ([^:]+): (.*)$")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / "tools/hermes/hermes-home"))


def setting(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        for line in (hermes_home() / ".env").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def ffmpeg_binary() -> str | None:
    found = os.environ.get("HERMES_MEET_FFMPEG") or shutil.which("ffmpeg")
    if found:
        return found
    bundled = sorted(glob.glob(str(hermes_home() / "tools/ffmpeg-*/bin/ffmpeg")))
    return bundled[-1] if bundled else None


def split_audio(audio: Path, workdir: Path) -> list[Path]:
    if audio.stat().st_size < 20 * 1024 * 1024:
        return [audio]
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise RuntimeError("audio is over 20 MB and ffmpeg is missing to split it")
    pattern = workdir / "part%03d.ogg"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio), "-f", "segment",
                    "-segment_time", str(CHUNK_SECONDS), "-c", "copy", str(pattern)], check=True)
    return sorted(workdir.glob("part*.ogg"))


def transcribe_chunk(path: Path, key: str) -> list[dict]:
    import requests
    data = {"model": setting("MEET_STT_MODEL", "whisper-large-v3-turbo"),
            "response_format": "verbose_json", "temperature": "0"}
    language = setting("MEET_STT_LANGUAGE")
    if language:
        data["language"] = language
    for attempt in range(4):
        with path.open("rb") as f:
            resp = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                                 data=data, files={"file": (path.name, f, "audio/ogg")}, timeout=600)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(min(60, 10 * (attempt + 1)))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("segments") or []
    raise RuntimeError(f"Groq HTTP {resp.status_code} after retries: {resp.text[:300]}")


def caption_speakers(captions: Path, day_start: float) -> list[tuple[float, str]]:
    """(epoch seconds, speaker) for caption lines that name a real speaker."""
    out = []
    if not captions.is_file():
        return out
    base = time.localtime(day_start)
    for line in captions.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CAPTION_RE.match(line)
        if not m or m.group(4).strip().lower() in {"unknown", "you"}:
            continue
        ts = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), 0, 0, -1))
        if ts < day_start - 3600:  # call crossed midnight
            ts += 86400
        out.append((ts, m.group(4).strip()))
    return out


def speaker_for(start: float, end: float, speakers: list[tuple[float, str]]) -> str:
    votes = Counter(name for ts, name in speakers if start - 2 <= ts <= end + 3)
    return votes.most_common(1)[0][0] if votes else ""


def main(out_dir: Path) -> int:
    status_file = out_dir / "transcribe.json"

    def status(**kw) -> None:
        status_file.write_text(json.dumps({**kw, "at": time.time()}, indent=1))

    audio = out_dir / "audio.ogg"
    if not audio.is_file() or audio.stat().st_size < 10_000:
        status(status="skipped", reason="no audio")
        return 0
    key = setting("GROQ_API_KEY")
    if not key:
        status(status="failed", error="GROQ_API_KEY is not set")
        return 1
    try:
        started = float(json.loads((out_dir / "recording.json").read_text())["started_at"])
    except (OSError, ValueError, KeyError):
        started = audio.stat().st_mtime  # rough
    captions = out_dir / "captions.txt"
    live = out_dir / "transcript.txt"
    if live.is_file() and not captions.is_file():
        live.replace(captions)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            parts = split_audio(audio, Path(tmp))
            speakers = caption_speakers(captions, started)
            lines, offset = [], 0.0
            for part in parts:
                segments = transcribe_chunk(part, key)
                for seg in segments:
                    text = str(seg.get("text", "")).strip()
                    if not text:
                        continue
                    t0, t1 = started + offset + float(seg.get("start", 0)), started + offset + float(seg.get("end", 0))
                    who = speaker_for(t0, t1, speakers)
                    stamp = time.strftime("%H:%M:%S", time.localtime(t0))
                    lines.append(f"[{stamp}] {who}: {text}" if who else f"[{stamp}] {text}")
                offset += CHUNK_SECONDS
    except Exception as exc:  # keep the captions as the transcript
        if captions.is_file() and not live.is_file():
            shutil.copyfile(captions, live)
        status(status="failed", error=str(exc)[:500])
        print(f"meet_transcribe: failed: {exc}", file=sys.stderr)
        return 1
    live.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    status(status="done", lines=len(lines), parts=len(parts),
           named=sum(1 for ln in lines if re.match(r"^\[[\d:]+\] [^:\[]+: ", ln)))
    print(f"meet_transcribe: {len(lines)} lines written to {live}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
