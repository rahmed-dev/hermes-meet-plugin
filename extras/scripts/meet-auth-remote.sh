#!/usr/bin/env bash
# One-time Google sign-in for the Hermes Meet bot on a server with no screen.
#
#   meet-auth-remote.sh start   Start Chromium in a virtual screen (Xvfb) with remote
#                               debugging on 127.0.0.1:9222, open the Google sign-in page.
#   meet-auth-remote.sh save    Check the sign-in, save a copy to workspace/meetings/auth.json
#                               (mode 600), stop Chromium and Xvfb. The profile stays in
#                               workspace/meetings/chrome-profile: the Meet bot runs with it.
#   meet-auth-remote.sh stop    Stop Chromium and Xvfb without saving.
#
# Between start and save, on the laptop:
#   ssh -L 9222:127.0.0.1:9222 riz@<server>
#   open edge://inspect in Edge, click "inspect" under the Google page, sign in on the live view.
set -euo pipefail

HERMES_HOME=/home/riz/tools/hermes/hermes-home
STATE=/home/riz/tools/hermes/state/meet-auth
# Since 2026-11-24 the bot uses this persistent profile, so Google's cookie rotation is kept.
PROFILE="$HERMES_HOME/workspace/meetings/chrome-profile"
AUTH_JSON="$HERMES_HOME/workspace/meetings/auth.json"
CHROME=/home/riz/.hermes/tools/chromium-1248/chrome-linux64/chrome
PY=/home/riz/tools/hermes/hermes-home/installs/5b04614cbb562921/environments/197f65070b444067a255d774f6f28d34/venv/bin/python
DISPLAY_NUM=:117
PORT=9222

stop_all() {
  for name in chrome xvfb; do
    if [[ -f "$STATE/$name.pid" ]]; then
      kill "$(cat "$STATE/$name.pid")" 2>/dev/null || true
      rm -f "$STATE/$name.pid"
    fi
  done
}

case "${1:-}" in
  start)
    mkdir -p "$STATE" "$PROFILE"
    chmod 700 "$STATE" "$PROFILE"
    if pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null; then
      echo "A Meet bot (Chromium) uses the profile now; wait until it ends" >&2
      exit 1
    fi
    if ss -ltn | grep -q "127.0.0.1:$PORT "; then
      echo "port $PORT is already in use; run '$0 stop' first" >&2
      exit 1
    fi
    setsid Xvfb "$DISPLAY_NUM" -screen 0 1280x900x24 -nolisten tcp >"$STATE/xvfb.log" 2>&1 &
    echo $! >"$STATE/xvfb.pid"
    sleep 1
    DISPLAY="$DISPLAY_NUM" setsid "$CHROME" \
      --user-data-dir="$PROFILE" \
      --remote-debugging-address=127.0.0.1 --remote-debugging-port="$PORT" \
      --no-sandbox --no-first-run --no-default-browser-check --window-size=1280,900 \
      --password-store=basic --use-mock-keychain \
      https://accounts.google.com/ >"$STATE/chrome.log" 2>&1 &
    echo $! >"$STATE/chrome.pid"
    sleep 3
    if curl -fs "http://127.0.0.1:$PORT/json/version" >/dev/null; then
      echo "Chromium is ready on 127.0.0.1:$PORT (display $DISPLAY_NUM)."
    else
      echo "Chromium did not open the debugging port; see $STATE/chrome.log" >&2
      stop_all
      exit 1
    fi
    ;;
  save)
    HERMES_HOME="$HERMES_HOME" "$PY" - "$AUTH_JSON" "$PORT" <<'EOF'
import json, os, sys
from playwright.sync_api import sync_playwright
path, port = sys.argv[1], sys.argv[2]
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    state = browser.contexts[0].storage_state()
names = {c["name"] for c in state["cookies"] if c["domain"].endswith("google.com")}
if not {"SID", "SAPISID"} & names:
    sys.exit("No Google sign-in cookies found. Finish the sign-in, then run save again.")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(state, fh)
os.replace(tmp, path)
print(f"saved {len(state['cookies'])} cookies to {path}")
EOF
    stop_all
    sleep 2
    echo "Chromium stopped. Profile kept in $PROFILE for the Meet bot."
    ;;
  stop)
    stop_all
    echo "stopped (nothing saved; profile kept in $PROFILE)"
    ;;
  *)
    echo "usage: $0 start|save|stop" >&2
    exit 2
    ;;
esac
