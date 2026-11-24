---
name: google-workspace
description: "Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python."
version: 1.2.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials (downloaded from Google Cloud Console)
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya]
---

# Google Workspace

Gmail, Calendar, Drive, Contacts, Sheets, and Docs — through Hermes-managed OAuth and a thin CLI wrapper. When `gws` is installed, the skill uses it as the execution backend for broader Google Workspace coverage; otherwise it falls back to the bundled Python client implementation.

## References

- `references/gmail-search-syntax.md` — Gmail search operators (is:unread, from:, newer_than:, etc.)
- `references/meet-join-from-calendar.md` — join a Meet that is running now: get the link from the event, drive `meet_join`, and read the bot's status/leave reason correctly (the "host denied admission" label covers three different failures).
- `references/daily-brief.md` — daily/morning brief procedure: schedule + conflicts + meeting prep + urgent mail from Gmail and Calendar. Load it when the user asks for a morning brief, meeting preparation, or "what's on my calendar and what email needs attention."

## Scripts

- `scripts/setup.py` — OAuth2 setup (run once to authorize)
- `scripts/google_api.py` — compatibility wrapper CLI. It prefers `gws` for operations when available, while preserving Hermes' existing JSON output contract.
- `scripts/meet_now.py` — events around now **from the owner's calendar id** (`MEET_CALENDAR_ID`), one line per event with a `LIVE` flag and the `hangoutLink`. Use this for "join the meeting on now"; `google_api.py calendar list` alone returns the bot's empty `primary` calendar and prints `[]`. Flags: `--before 3 --after 6 --json --cal <id>`.

```bash
HH=${HERMES_HOME:-$HOME/tools/hermes/hermes-home}
PY=$(ls -d $HH/installs/*/environments/*/venv/bin/python | head -1)
HERMES_HOME=$HH $PY $HH/skills/productivity/google-workspace/scripts/meet_now.py
```

### Run them with the Hermes venv python, not system python

The host's default `python` has neither the Google client libs nor Hermes importable, so
`python scripts/setup.py --check` fails with `ERROR: Run this script in the Hermes environment`
or `ModuleNotFoundError: No module named 'googleapiclient'`. The deps live in the committed PM venv:

```bash
HH=${HERMES_HOME:-$HOME/tools/hermes/hermes-home}
PY=$(ls -d $HH/installs/*/environments/*/venv/bin/python | head -1)
HERMES_HOME=$HH $PY setup.py --check     # AUTHENTICATED: Token valid at .../google_token.json
HERMES_HOME=$HH $PY google_api.py calendar list --start ... --end ...
```

Every environment hash carries the same deps — any of them works; if none imports
`googleapiclient`, run `setup.py --install-deps` from one that can import Hermes.
A failing `--check` is a wrong-interpreter problem, not a broken auth setup.

## Read-only calendar, no OAuth: the secret iCal address

When the job is only to **read** events — today's agenda, a morning brief, a watcher that acts on
upcoming meetings — skip the OAuth setup below entirely. Google Calendar publishes a private feed:
Calendar → Settings → the calendar → "Secret address in iCal format". Store it in `$HERMES_HOME/.env`
(usage-specific name, e.g. `MEET_ICAL_URL`) and `chmod 600` — anyone holding the URL reads that
calendar. No Google Cloud project, no consent screen, no token file.

```python
import icalendar, recurring_ical_events
cal = icalendar.Calendar.from_ical(raw)          # raw = urllib.request.urlopen(feed).read()
events = recurring_ical_events.of(cal).between(now - timedelta(days=1), now + timedelta(days=7))
```

Install `icalendar` + `recurring-ical-events` into the venv the code will actually run on: for a cron
`.py` script that is the committed PM generation (`installs/<id>/environments/<hash>/venv`), which can
be a different hash from the live agent's venv.

Pitfalls that cost real time:

- Recurring meetings are RRULEs on one VEVENT — expand with `recurring_ical_events`, never by reading
  VEVENTs directly, or a daily standup appears once a week at best.
- All-day events carry `DTSTART` as a `date`, not a `datetime` — check the type before comparing to a
  `now` datetime, or the comparison raises.
- A Meet/Zoom link sits in `X-GOOGLE-CONFERENCE`, else in DESCRIPTION or LOCATION; regex it out per event.
- Events the owner declined are still in the feed, with a `Declined:` SUMMARY prefix (and
  `PARTSTAT=DECLINED` on the ATTENDEE) — filter them or the watcher joins calls he skipped.
- Only the calendars whose secret address was added are covered; say so instead of implying "my calendar".
- ICS carries no write access and little per-attendee state — that, and only that, is when the OAuth
  setup below earns its 5-minute Google Cloud detour.

## First-Time Setup

The setup is fully non-interactive — you drive it step by step so it works
on CLI, Telegram, Discord, or any platform.

Run the setup script with Python from the Hermes environment, not an unrelated
system Python. `--install-deps` syncs Hermes' declared Google extra through PM;
after syncing, restart Hermes and rerun the OAuth command. If Hermes is not
importable, use `hermes setup` first rather than installing packages with pip.

Define a shorthand first:

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
```

### Step 0: Check if already set up

```bash
$GSETUP --check
```

If it prints `AUTHENTICATED`, skip to Usage — setup is already done.

### Step 1: Triage — ask the user what they need

Before starting OAuth setup, ask the user TWO questions:

**Question 1: "What Google services do you need? Just email, or also
Calendar/Drive/Sheets/Docs?"**

- **Email only** → They don't need this skill at all. Use the `himalaya` skill
  instead — it works with a Gmail App Password (Settings → Security → App
  Passwords) and takes 2 minutes to set up. No Google Cloud project needed.
  Load the himalaya skill and follow its setup instructions.

- **Email + Calendar** → Continue with this skill, but use
  `--services email,calendar` during auth so the consent screen only asks for
  the scopes they actually need.

- **Read-only calendar** (agenda, morning brief, meeting watcher) → do NOT start
  the OAuth setup at all; use the secret iCal address section above.

- **Calendar/Drive/Sheets/Docs only** → Continue with this skill and use a
  narrower `--services` set like `calendar,drive,sheets,docs`.

- **Full Workspace access** → Continue with this skill and use the default
  `all` service set.

**Question 2: "Does your Google account use Advanced Protection (hardware
security keys required to sign in)? If you're not sure, you probably don't
— it's something you would have explicitly enrolled in."**

- **No / Not sure** → Normal setup. Continue below.
- **Yes** → Their Workspace admin must add the OAuth client ID to the org's
  allowed apps list before Step 4 will work. Let them know upfront.

### Step 2: Create OAuth credentials (one-time, ~5 minutes)

Tell the user:

> You need a Google Cloud OAuth client. This is a one-time setup:
>
> 1. Create or select a project:
>    https://console.cloud.google.com/projectselector2/home/dashboard
> 2. Enable the required APIs from the API Library:
>    https://console.cloud.google.com/apis/library
>    Enable: Gmail API, Google Calendar API, Google Drive API,
>    Google Sheets API, Google Docs API, People API
> 3. Create the OAuth client here:
>    https://console.cloud.google.com/apis/credentials
>    Credentials → Create Credentials → OAuth 2.0 Client ID
> 4. Application type: "Desktop app" → Create
> 5. If the app is still in Testing, add the user's Google account as a test user here:
>    https://console.cloud.google.com/auth/audience
>    Audience → Test users → Add users
> 6. Download the JSON file and tell me the file path
>
> Important Hermes CLI note: if the file path starts with `/`, do NOT send only the bare path as its own message in the CLI, because it can be mistaken for a slash command. Send it in a sentence instead, like:
> `The JSON file path is: ~/Downloads/client_secret_....json`

Once they provide the path:

```bash
$GSETUP --client-secret /path/to/client_secret.json
```

If they paste the raw client ID / client secret values instead of a file path,
write a valid Desktop OAuth JSON file for them yourself, save it somewhere
explicit (for example `~/Downloads/hermes-google-client-secret.json`), then run
`--client-secret` against that file.

### Step 3: Get authorization URL

Use the service set chosen in Step 1. Examples:

```bash
$GSETUP --auth-url --services email,calendar --format json
$GSETUP --auth-url --services calendar,drive,sheets,docs --format json
$GSETUP --auth-url --services all --format json
```

This returns JSON with an `auth_url` field and also saves the exact URL to
`~/.hermes/google_oauth_last_url.txt`.

Agent rules for this step:
- Extract the `auth_url` field and send that exact URL to the user as a single line.
- Tell the user that the browser will likely fail on `http://localhost:1` after approval, and that this is expected.
- Tell them to copy the ENTIRE redirected URL from the browser address bar.
- If the user gets `Error 403: access_denied`, send them directly to `https://console.cloud.google.com/auth/audience` to add themselves as a test user.

### Step 4: Exchange the code

The user will paste back either a URL like `http://localhost:1/?code=4/0A...&scope=...`
or just the code string. Either works. The `--auth-url` step stores a temporary
pending OAuth session locally so `--auth-code` can complete the PKCE exchange
later, even on headless systems:

```bash
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED" --format json
```

If `--auth-code` fails because the code expired, was already used, or came from
an older browser tab, it now returns a fresh `fresh_auth_url`. In that case,
immediately send the new URL to the user and have them retry with the newest
browser redirect only.

### Step 5: Verify

```bash
$GSETUP --check
```

Should print `AUTHENTICATED`. Setup is complete — token refreshes automatically from now on.

### Notes

- Token is stored at `~/.hermes/google_token.json` and auto-refreshes.
- Pending OAuth session state/verifier are stored temporarily at `~/.hermes/google_oauth_pending.json` until exchange completes.
- If `gws` is installed, `google_api.py` points it at the same `~/.hermes/google_token.json` credentials file. Users do not need to run a separate `gws auth login` flow.
- To revoke: `$GSETUP --revoke`

## Usage

All commands go through the API script. Set `GAPI` as a shorthand:

```bash
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
```

### Gmail

```bash
# Search (returns JSON array with id, from, subject, date, snippet)
$GAPI gmail search "is:unread" --max 10
$GAPI gmail search "from:boss@company.com newer_than:1d"
$GAPI gmail search "has:attachment filename:pdf newer_than:7d"

# Read full message (returns JSON with body text)
$GAPI gmail get MESSAGE_ID

# Send
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"
$GAPI gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1><p>Details...</p>" --html
$GAPI gmail send --to user@example.com --subject "Hello" --from '"Research Agent" <user@example.com>' --body "Message text"

# Reply (automatically threads and sets In-Reply-To)
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
$GAPI gmail reply MESSAGE_ID --from '"Support Bot" <user@example.com>' --body "Thanks"

# Labels
$GAPI gmail labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
$GAPI gmail modify MESSAGE_ID --remove-labels UNREAD
```

### Calendar

```bash
# List events (defaults to next 7 days)
$GAPI calendar list
$GAPI calendar list --start 2026-03-01T00:00:00Z --end 2026-03-07T23:59:59Z

# Create event (ISO 8601 with timezone required)
$GAPI calendar create --summary "Team Standup" --start 2026-03-01T10:00:00-06:00 --end 2026-03-01T10:30:00-06:00
$GAPI calendar create --summary "Lunch" --start 2026-03-01T12:00:00Z --end 2026-03-01T13:00:00Z --location "Cafe"
$GAPI calendar create --summary "Review" --start 2026-03-01T14:00:00Z --end 2026-03-01T15:00:00Z --attendees "alice@co.com,bob@co.com"

# Delete event
$GAPI calendar delete EVENT_ID
```

Pitfalls that produce wrong answers:

- **A bare `calendar list` reads the authenticated account's own `primary` calendar. For a bot account
  that is empty, and `[]` does not mean "no events".** Enumerate `service.calendarList().list()` to see
  what the token can actually read, then query the owner's calendar id explicitly — on this host it is
  stored as `MEET_CALENDAR_ID` in `$HERMES_HOME/.env` (the owner's personal calendar, shared with the
  bot account; the bot's `primary` stays empty). A calendar shared with the bot can be readable by id
  even when it does not appear in `calendarList` — query the id first, and only then conclude there is
  no access.
- Always fetch with `singleEvents=True, orderBy='startTime'` for an agenda; recurring standups are one
  VEVENT otherwise.
- Print `attendees` alongside the times. Invitation state decides what is possible later: it is the
  difference between a Meet bot that can join as an invited identity and one stuck in the guest lobby.
- Save the event's `hangoutLink` (fallback `conferenceData.entryPoints[type=video].uri`) when the ask is
  "join the meeting that is running now" — use `scripts/meet_now.py` to get it, then follow
  `references/meet-join-from-calendar.md`.
- "Schedule a meeting and send me the link" (LOCAL PATCH, 2026-11-24): create the event on the bot's
  own calendar with Riz as guest, a Meet link and an emailed invitation:
  `google_api.py calendar create --summary "<title>" --start <ISO> --end <ISO> --attendees <Riz's personal Gmail> --meet --notify`.
  Since the third 2026-11-24 patch, a default `calendar create` (no `--calendar`) puts the event on
  Riz's own calendar, so Riz is the organizer, and adds the bot (`HERMES_BOT_EMAIL`) as a guest.
  Riz shared his calendar with the bot with write access for this. If that insert is refused, it
  falls back to the bot's calendar with Riz as guest. Earlier note, still true for the fallback:
  `calendar create` always adds `HERMES_OWNER_EMAIL` (Riz) as a
  guest and always sends the invite, even without `--attendees`/`--notify`. Still pass other
  guests (for example a colleague) with `--attendees`.
  Post the printed `meetLink` in the chat. The event then shows on Riz's calendar, so the Meet
  watcher joins it automatically. The bot is the organizer, so it joins without the lobby.
  The `--attendees … --notify` part is not optional: `<bot Gmail, HERMES_BOT_EMAIL>` has only **read** access
  to Riz's calendar (`<owner Gmail, HERMES_OWNER_EMAIL>`), so a plain `events.insert` on the bot's `primary`
  never reaches Riz — he says "this did not show on my calendar". Do not invent a workaround when the
  invite path fails; give him a
  `https://calendar.google.com/calendar/render?action=TEMPLATE&text=…&dates=…Z/…Z&details=…&location=…`
  link (one tap, then Save) or ask him to re-share the calendar with the bot as "Make changes to events".

### Drive

```bash
# Search existing files
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5

# Get metadata for a single file
$GAPI drive get FILE_ID

# Upload a local file (auto-detects MIME type)
$GAPI drive upload /path/to/report.pdf
$GAPI drive upload /path/to/image.png --name "Logo.png" --parent FOLDER_ID

# Download (binary files download as-is; Google-native files export to a
# sensible default — Docs→pdf, Sheets→csv, Slides→pdf, Drawings→png)
$GAPI drive download FILE_ID
$GAPI drive download DOC_ID --output ~/doc.pdf
$GAPI drive download DOC_ID --export-mime text/plain --output ~/doc.txt

# Create a folder
$GAPI drive create-folder "Reports"
$GAPI drive create-folder "Q4" --parent FOLDER_ID

# Share
$GAPI drive share FILE_ID --email alice@example.com --role reader
$GAPI drive share FILE_ID --email alice@example.com --role writer --notify
$GAPI drive share FILE_ID --type anyone --role reader        # anyone with link
$GAPI drive share FILE_ID --type domain --domain example.com --role reader

# Delete — defaults to trash (reversible). Use --permanent to skip the trash.
$GAPI drive delete FILE_ID
$GAPI drive delete FILE_ID --permanent
```

### Contacts

```bash
$GAPI contacts list --max 20
```

### Sheets

```bash
# Create a new spreadsheet
$GAPI sheets create --title "Q4 Budget"
$GAPI sheets create --title "Inventory" --sheet-name "Stock"

# Read
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"

# Write
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'

# Append rows
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

### Docs

```bash
# Read (a tabbed Doc returns a "tabs" array; single-tab and legacy Docs also return "body")
$GAPI docs get DOC_ID
$GAPI docs get DOC_ID --tab TAB_ID     # read one tab of a tabbed Doc

# Create a new Doc (optionally seeded with body text)
$GAPI docs create --title "Meeting Notes"
$GAPI docs create --title "Draft" --body "First paragraph..."

# Append text to the end of an existing Doc
$GAPI docs append DOC_ID --text "Additional content to append"
$GAPI docs append DOC_ID --tab TAB_ID --text "..."   # --tab required when the Doc has multiple tabs
```

## Output Format

All commands return JSON. Parse with `jq` or read directly. Key fields:

- **Gmail search**: `[{id, threadId, from, to, subject, date, snippet, labels}]`
- **Gmail get**: `{id, threadId, from, to, subject, date, labels, body}`
- **Gmail send/reply**: `{status: "sent", id, threadId}`
- **Calendar list**: `[{id, summary, start, end, location, description, htmlLink}]`
- **Calendar create**: `{status: "created", id, summary, htmlLink}`
- **Drive search**: `[{id, name, mimeType, modifiedTime, webViewLink}]`
- **Drive get**: `{id, name, mimeType, modifiedTime, size, webViewLink, parents, owners}`
- **Drive upload**: `{status: "uploaded", id, name, mimeType, webViewLink}`
- **Drive download**: `{status: "downloaded", id, name, path, mimeType}`
- **Drive create-folder**: `{status: "created", id, name, webViewLink}`
- **Drive share**: `{status: "shared", permissionId, fileId, role, type}`
- **Drive delete**: `{status: "trashed" | "deleted", fileId, permanent}`
- **Contacts list**: `[{name, emails: [...], phones: [...]}]`
- **Sheets get**: `[[cell, cell, ...], ...]`
- **Sheets create**: `{status: "created", spreadsheetId, title, spreadsheetUrl}`
- **Docs create**: `{status: "created", documentId, title, url}`
- **Docs append**: `{status: "appended", documentId, inserted_at, characters}`

## Rules

1. **Never send email, create/delete calendar events, delete Drive files, share files, or modify Docs/Sheets without confirming with the user first.** Show what will be done (recipients, file IDs, content, share role) and ask for approval. For `drive delete`, prefer the default trash (reversible) over `--permanent`.
2. **Check auth before first use** — run `setup.py --check`. If it fails, guide the user through setup.
3. **Use the Gmail search syntax reference** for complex queries — load it with `skill_view("google-workspace", file_path="references/gmail-search-syntax.md")`.
4. **Calendar times must include timezone** — always use ISO 8601 with offset (e.g., `2026-03-01T10:00:00-06:00`) or UTC (`Z`).
5. **Respect rate limits** — avoid rapid-fire sequential API calls. Batch reads when possible.
6. **A meeting Riz asks for goes on HIS calendar, not the bot's.** `events.insert` on the bot's
   `primary` only reaches Riz if you pass him as a guest with email delivery: `google_api.py calendar
   create --summary "<title>" --start <ISO+P5> --end <ISO+P5> --attendees <owner Gmail, HERMES_OWNER_EMAIL>
   --meet --notify`. The bot has read-only access to his calendar, so there is no other write path; a
   bare insert produces a link Riz can call from but never sees in his calendar. Post the printed
   `meetLink` in chat with the time in PKT. Do not hand-write a `conferenceData` script — `--meet --notify`
   already exists in `google_api.py`.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `NOT_AUTHENTICATED` | Run setup Steps 2-5 above |
| `REFRESH_FAILED` | Token revoked or expired — redo Steps 3-5 |
| `HttpError 403: Insufficient Permission` | Missing API scope — `$GSETUP --revoke` then redo Steps 3-5 |
| `AUTHENTICATED (partial)` or "Token missing scopes" | New write capabilities (Drive write/delete, Docs create/edit) require re-authorization. `$GSETUP --revoke` then redo Steps 3-5 to grant the upgraded scopes. |
| `HttpError 403: Access Not Configured` | API not enabled — user needs to enable it in Google Cloud Console |
| `ModuleNotFoundError` | Run `$GSETUP --install-deps` |
| `ERROR: Run this script in the Hermes environment` | Wrong interpreter — run the scripts with the venv python from `installs/*/environments/*/venv/bin/python` and `HERMES_HOME` set, see Scripts above |
| Advanced Protection blocks auth | Workspace admin must allowlist the OAuth client ID |

## Revoking Access

```bash
$GSETUP --revoke
```
