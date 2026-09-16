# Miscellaneous Ken

## Mac mini app

The project now includes **Miscellaneous Ken.app**, a native Mac window and menu-bar entry with a local Python background service. See [the app guide](deploy/STUDIO.md) for installation, API connections, video editing, and automation. The older n8n and calendar scheduling instructions below are retained for reference; the app installer replaces those launchd jobs with one background service.

An automated short-form pipeline: find a trending story, have Claude write a
headline for it, composite the source clip into the channel's card format, and
post it to YouTube Shorts. No manual approval step — it is meant to run on a
schedule.

```
discover  →  pick (Claude)  →  download (yt-dlp)  →  render  →  upload
   RSS         editorial          source clip        1080×1920      Shorts
   Reddit      system prompt                          card
   YouTube*
```

## The card format

A black 1080×1920 canvas with the content block vertically centred:

- **Header** — circular avatar, account name, blue verified tick, `@handle`
- **Headline** — bold monospace, all-caps, word-wrapped to the canvas width
- **Clip** — the source video in a rounded rectangle with a blue border

Everything except the video pixels is drawn once by Pillow into a transparent
PNG; ffmpeg then cover-crops the clip into the window, rounds its corners with
a generated alpha mask, and lays the PNG on top.

## Files

| File | What it does |
| --- | --- |
| `render.py` | Card compositor. Usable standalone or imported by `agent.py`. |
| `agent.py` | The pipeline. `--dry-run` renders without posting. |
| `youtube_auth.py` | One-time OAuth → writes `token.json`. |
| `runner.py` | HTTP bridge so n8n-in-Docker can trigger a run on the host. |
| `config.json` | Account, feeds, editorial prompt, posting + layout settings. |
| `state.json` | Auto-created. Every URL seen/posted, so nothing repeats. |
| `work/`, `out/` | Downloads + overlays; finished mp4s and poster frames. |

## Setup

**1. System tools**

```bash
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Debian/Ubuntu
ffmpeg -version              # confirm it's on PATH
```

**2. Python deps** (a venv keeps this off your system Python)

```bash
cd miscellaneous-ken
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Anthropic API key**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Put it in your shell profile, or in a `.env` you source — and if you drive this
from n8n, set it in the node's environment rather than hardcoding it.

**4. YouTube OAuth** (once)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → fill in the basics → add scope
   `https://www.googleapis.com/auth/youtube.upload` → add the channel's Google
   account under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** →
   download the JSON → save it here as `client_secret.json`.
5. Run it:

   ```bash
   python3 youtube_auth.py
   ```

   A browser opens; approve, and `token.json` lands in this folder. `agent.py`
   refreshes it from then on.

> While the consent screen is in **Testing**, refresh tokens expire after 7
> days and you'll have to re-run `youtube_auth.py` weekly. Hitting **Publish
> app** stops that. An unverified app is fine for a personal channel.

**5. Avatar**

Drop a square PNG at `assets/avatar.png` (or point `account.avatar` elsewhere).
Without one, the renderer draws a lettered placeholder disc.

## Running

```bash
# render everything, upload nothing — start here
python3 agent.py --dry-run -v

# render one clip without calling the Claude API at all
python3 agent.py --dry-run --no-llm --limit 1 -v

# the real thing
python3 agent.py
```

Render a card by hand, without the pipeline:

```bash
python3 render.py --video some_clip.mp4 \
  --headline "A thing happened somewhere" \
  --out out/manual.mp4 --poster out/manual.png
```

Check `out/*.png` before you trust a run — the poster frame shows exactly what
the card looks like.

## Scheduling with n8n (Docker)

Discovery and story-picking deliberately live inside `agent.py`, so n8n's only
job is to start a run on a schedule.

**n8n runs in Docker, which changes how that works.** An Execute Command node
runs *inside the container*, and the container cannot see
`~/projects/miscellaneous-ken`, has no python/ffmpeg/yt-dlp, and has no copy of
your YouTube OAuth token. So instead:

```
n8n (docker)  --POST http://host.docker.internal:8765/run-->  runner.py  -->  agent.py
```

`runner.py` is a ~150-line stdlib HTTP service that runs on the Mac itself, with
your real Python, ffmpeg and `token.json`. No custom image, no bind mounts, and
the OAuth flow stays on the host where a browser can open.

**Start it:**

```bash
cd ~/projects/miscellaneous-ken
export MKEN_RUNNER_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export ANTHROPIC_API_KEY="sk-ant-..."
.venv/bin/python3 runner.py
```

Check it:

```bash
curl localhost:8765/health
curl -X POST -H "X-Runner-Token: $MKEN_RUNNER_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"dry_run":true,"limit":1}' localhost:8765/run
```

The n8n workflow (**Miscellaneous Ken — auto-post to YouTube Shorts**) is:

```
Schedule Trigger (09:00 / 14:00 / 19:00 Accra)
  → HTTP Request  POST host.docker.internal:8765/run
  → IF  $json.ok
      true  → Posted (no-op)
      false → Telegram alert with the tail of the log
```

The `X-Runner-Token` header value in the HTTP Request node must match
`MKEN_RUNNER_TOKEN` on the host.

### Security note on the bind address

`runner.py` binds `0.0.0.0`, not `127.0.0.1`. It has to: traffic arriving from
Docker Desktop via `host.docker.internal` does not come from loopback, so a
loopback bind would refuse the container. That means anything on your local
network can reach port 8765 — which is why the token is mandatory and the
service refuses to start without one. On a network you don't trust, firewall
the port or bind it to the Docker bridge interface specifically
(`MKEN_RUNNER_HOST`).

### Keeping it running

The runner has to be up when the schedule fires. For a launchd job that starts
it at login and restarts it if it dies, create
`~/Library/LaunchAgents/com.miscellaneousken.runner.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.miscellaneousken.runner</string>
  <key>ProgramArguments</key><array>
    <string>/Users/dankwawadie/projects/miscellaneous-ken/.venv/bin/python3</string>
    <string>/Users/dankwawadie/projects/miscellaneous-ken/runner.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/dankwawadie/projects/miscellaneous-ken</string>
  <key>EnvironmentVariables</key><dict>
    <key>MKEN_RUNNER_TOKEN</key><string>PUT_YOUR_TOKEN_HERE</string>
    <key>ANTHROPIC_API_KEY</key><string>sk-ant-...</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/mken-runner.log</string>
  <key>StandardErrorPath</key><string>/tmp/mken-runner.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.miscellaneousken.runner.plist
```

That plist holds your API key in plaintext in your home folder — `chmod 600` it.

### If you'd rather not run a service

The alternative is to let **launchd do the scheduling too** (a `StartCalendarInterval`
job running `agent.py` directly) and use n8n only for notification, via a Webhook
node that `agent.py` POSTs to. Fewer moving parts and nothing listening on a
port; the cost is that the schedule no longer lives in the n8n UI.

### If you later want discovery in n8n

The seam to cut is `discover()` / `pick_stories()` — everything downstream takes
a `Pick` and doesn't care where it came from.

## Quota

The YouTube Data API gives a new project **10,000 units/day** by default. An
upload costs **1,600 units**, so that's **~6 uploads per day** — and a
`search.list` call costs 100 units on top, which is why `youtube_queries` is
empty in `config.json` by default. `clips_per_run: 1` on an hourly schedule
will blow through the quota by mid-morning; pick a cadence that lands under six
a day, or request a quota increase in the Cloud Console.

Quota resets at midnight Pacific, not local midnight.

## Content safety and copyright

**Reposting other people's footage is the main risk in this whole pipeline.**
It is not a theoretical one — YouTube's Content ID and manual claim system is
aggressive, and three copyright strikes terminates a channel.

Practical mitigations, in rough order of how much they help:

- **Prefer footage you have a right to use.** Public-domain and government
  sources (NASA, NOAA, ESA), Creative Commons uploads, and clips whose owners
  publish reuse terms are far safer than a random viral video.
- **Credit the source in the description** — `agent.py` already appends the
  original URL and where it was found. Credit is not a licence, but it matters
  if you ever have to argue good faith, and it's the decent thing to do.
- **Keep clips short.** `max_clip_seconds` defaults to 30. Shorter excerpts sit
  closer to fair use / fair dealing, though there is no safe-harbour duration —
  the "7 seconds is fine" thing is a myth.
- **Honour takedowns immediately** and don't re-upload the same source.
- **Don't monetise contested clips.** A claim on a monetised video redirects
  revenue and raises the stakes of a dispute.
- **Consider transformation.** Commentary, added context, or your own voiceover
  strengthens a fair-use argument considerably; a bare repost of someone's clip
  with a headline over it is the weakest position.

Fair use is a defence, decided case by case — not a permission you can grant
yourself in advance. If the channel matters to you, treat the source list as
something to curate deliberately rather than something to point at whatever is
hot today. This isn't legal advice; if you're going to run this at scale or
monetise it, it's worth a conversation with someone who does this for a living.

The editorial system prompt also tells Claude to skip tragedy, graphic
material, and named private individuals in distress. Read it in `config.json`
and tighten it to taste — it's the only filter between a trending clip and your
channel.
