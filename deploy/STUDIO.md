# Miscellaneous Ken — Mac app

Open **Miscellaneous Ken** in `~/Applications`. The app has its own window, Dock icon, and **K.** menu-bar control. The interface is backed by a loopback-only service; nothing is hosted online.

## Everyday use

- **Overview:** Create preview makes a local draft. Run & upload creates videos and uploads using the current visibility setting.
- **Video library:** Watch and export videos. New drafts retain source media and editorial metadata. Save edits, render on-screen changes again, then upload. Older videos support title/description edits but have insufficient source metadata for rendering again.
- **Connections:** Save or replace Gemini, Anthropic, OpenAI, and YouTube discovery API keys. Configure provider order and model names. Upload a Google **Desktop app** OAuth JSON and connect/reconnect YouTube through your default browser. A saved key/token is not a verified connection; the provider validates it when used.
- **Content & style:** Edit channel identity, topics, channels, RSS context, editorial direction, headline size, border color, duration, and music volume. Unexposed existing config values are preserved.
- **Automation:** Enable/pause automatic runs, select previews or automatic uploads, set the interval and posting defaults. Pausing prevents future runs; an already-running job finishes.

The installer initially enables automatic uploads every **5 hours**, retaining the existing upload visibility (currently private). Choose **Create previews for review** if you prefer to approve each draft.

## Background behavior

The service starts at login and survives closing or quitting the app. It checks Internet connectivity every 30 seconds. When a run is due, online, and at least 512 MB of disk space is available, it starts one run and saves the next due time. A missed interval does not create a backlog. If a scheduled run fails, the next attempt is at the next interval; manual runs are available meanwhile.

The Mac must be awake and logged in. Internet connectivity does not wake a sleeping or powered-off laptop. Each active video job uses `caffeinate -i` to prevent idle sleep. A closed lid can still suspend it. Long jobs time out after 30 minutes.

The installer disables the previous `com.miscellaneousken.agent` and runner launchd registrations and retains their plists with `.disabled` suffixes. Do not reactivate external n8n schedules alongside this service. Manual pipeline CLI and studio workers use the same file lock.

If storage is low, free disk space using your normal Mac storage tools. No existing source clips or finished videos are automatically deleted.

## Install / rebuild

From the project directory:

```sh
bash deploy/build-app.sh
.venv/bin/python3 deploy/install-studio.py
open "$HOME/Applications/Miscellaneous Ken.app"
```

The app is compiled from `macos/Studio.m` using the Mac's developer tools and ad-hoc signed for this Mac. It is a local app, not a notarized distributable for other users. Keep the project folder and Python environment in place: the installed service refers to them.

After editing Python or interface files, restart the background service when no job is active:

```sh
launchctl kickstart -k "gui/$(id -u)/com.miscellaneousken.studio"
```

For normal use, pause automation in the app rather than stopping the service. To unload it entirely:

```sh
launchctl bootout "gui/$(id -u)/com.miscellaneousken.studio"
```

Re-run the installer to restore it. Logs are in `logs/studio.log` and `logs/studio.err.log`.

## Data and safeguards

- `studio.sqlite3` stores drafts, upload results, and job history; `state.json` continues to handle discovery deduplication.
- `studio-settings.json` stores automation preferences and the next due time.
- `studio-secrets.json` stores newly entered API keys with owner-only file permissions. They override `mken.env`; removing a key creates an empty override. Keys are never returned to the interface. These are local files, not encrypted Keychain entries.
- The service binds only to `127.0.0.1:8766`, checks Host/Origin and a per-process request token, and exposes only allowlisted settings and indexed media. It is intended for the current Mac user, not multiple accounts or remote access.
- Uploads with an uncertain result are held for review. Check YouTube Studio before marking one as not uploaded and retrying.
- Draft creation no longer increments uploaded counts. Actual failed attempts and unused editorial backups are counted separately.
- The existing `runner.py` HTTP bridge remains compatible but is no longer needed by the Mac app.

## Verification

```sh
.venv/bin/python3 -m unittest discover -s tests -v
node --check studio/app.js
bash deploy/build-app.sh
```

The tests isolate their data in temporary directories and mock AI calls, rendering, and uploads. They cover drafts, upload uncertainty, duplicate protection, cross-process locking, source configuration preservation, credential redaction, localhost protections, media ranges, offline scheduling, and low-storage scheduling. Real provider availability and end-to-end publishing require a live run.
