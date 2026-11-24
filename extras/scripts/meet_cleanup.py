#!/usr/bin/env python3
"""Delete old Google Meet bot files (LOCAL, Riz 2026-11-24).

Runs as a Hermes cron job (no agent). Looks only at meeting folders in
$HERMES_HOME/workspace/meetings/ whose name is a Meet code (abc-defg-hij).
In each, deletes files older than MEET_RETENTION_DAYS (default 30) by
modification time, then deletes the folder when it is empty. A folder with
a live bot (pid in status.json) is skipped. Never touches chrome-profile,
signin-profile, auth.json, watcher/, debug/ or any other non-meeting path.

stdout goes to Discord verbatim; empty stdout sends nothing.
Options: --dry-run (list only), --days N.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

MEET_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / "tools/hermes/hermes-home"))


def bot_alive(folder: Path) -> bool:
    try:
        pid = int(json.loads((folder / "status.json").read_text()).get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=float, default=float(os.environ.get("MEET_RETENTION_DAYS", "30")))
    args = parser.parse_args()

    root = hermes_home() / "workspace" / "meetings"
    if not root.is_dir():
        return 0
    cutoff = time.time() - args.days * 86400
    files = size = 0
    meetings: set[str] = set()
    removed_dirs = 0

    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not MEET_CODE.match(folder.name):
            continue
        if bot_alive(folder):
            continue
        # deepest paths first, so emptied subfolders can go too
        for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_symlink() or path.is_file():
                    st = path.lstat()
                    if st.st_mtime < cutoff:
                        if args.dry_run:
                            print(f"would delete {path.relative_to(root)}")
                        else:
                            path.unlink()
                        files += 1
                        size += st.st_size
                        meetings.add(folder.name)
                elif path.is_dir() and not args.dry_run and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                print(f"Meet cleanup: could not delete {path.relative_to(root)}: {exc}")
        if not args.dry_run:
            try:
                if not any(folder.iterdir()):
                    folder.rmdir()
                    removed_dirs += 1
            except OSError:
                pass

    if files:
        verb = "would delete" if args.dry_run else "deleted"
        line = (f"Meet cleanup: {verb} {files} files, {size / 1048576:.1f} MB, "
                f"from {len(meetings)} meetings (older than {args.days:g} days)")
        if removed_dirs:
            line += f"; {removed_dirs} empty meeting folders removed"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
