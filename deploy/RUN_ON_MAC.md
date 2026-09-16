# Running autonomously on this Mac (launchd)

This is the setup in use. It replaces n8n, Docker, ngrok and `runner.py` with a
single scheduled job:

```
launchd  →  deploy/run.sh  →  agent.py  →  Telegram alerts (from Python)
```

Four things that all had to be alive become one. Nothing listens on a port,
there is no bridge, no shared token, and no monthly bill.

**The trade-off, stated plainly:** the Mac must be powered on and awake at
09:00, 14:00 and 19:00. It is not a server. What you get in exchange is a
residential IP, which is the main reason YouTube downloads work here and often
fail on cloud hosts.

---

## Install

```bash
cd ~/projects/miscellaneous-ken
bash deploy/install-launchd.sh
```

It refuses to install until the venv, ffmpeg, `token.json` and `config.json`
are all present, and creates `mken.env` from the example if it's missing. Fill
that in and run it again:

```bash
nano mken.env      # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN
bash deploy/install-launchd.sh
```

For the Telegram token: message **@BotFather**, `/newbot`, follow the prompts.
Then **send your new bot a message yourself** — a bot cannot start a
conversation, so until you do, `sendMessage` fails with "chat not found".

## Verify, cheapest first

```bash
.venv/bin/python3 agent.py --notify-test        # alerting works
.venv/bin/python3 agent.py --check-sources      # channels resolve
bash deploy/run.sh --dry-run --limit 1          # exactly what launchd runs
tail -f logs/agent.log
```

That third command is the one that matters: it goes through `run.sh`, so it
proves the wrapper, the PATH and the env file — not just the Python.

Then a real run on demand:

```bash
launchctl kickstart -k gui/$(id -u)/com.miscellaneousken.agent
```

## Stop the old path

Leaving n8n running alongside this means **two schedulers, one YouTube quota
(~6 uploads/day), and two `state.json` files that know nothing about each
other** — so duplicate posts and an exhausted quota. Pick one.

- n8n: open the workflow and toggle it **inactive**.
- `runner.py`: Ctrl-C its terminal. Nothing else needs it now.

## Operating it

```bash
launchctl list | grep miscellaneousken            # is it loaded
tail -f logs/agent.log                            # what happened
launchctl kickstart -k gui/$(id -u)/com.miscellaneousken.agent   # run now
launchctl bootout gui/$(id -u)/com.miscellaneousken.agent        # pause
bash deploy/install-launchd.sh                    # resume / reinstall
```

Changing the schedule means editing the `StartCalendarInterval` block in
`deploy/com.miscellaneousken.agent.plist` and re-running the installer — it
unloads the old definition first, which launchd will otherwise keep using.

## Sleep, which is the real caveat

A MacBook with the lid closed runs nothing. Options, in order of how much you
care:

- **Accept it.** launchd runs a missed slot shortly after the Mac wakes, and
  nothing is marked as seen until it has actually rendered, so a skipped run
  costs you a post, not a story.
- **Keep it awake on power.** System Settings → Battery → Options → *Prevent
  automatic sleeping when the display is off*, plus Lock Screen → *Turn display
  off* set generously.
- **Lid closed, still running:** needs external power and a connected display
  or a dongle that mimics one. Worth it only if you want this genuinely
  unattended.

`run.sh` wraps each run in `caffeinate -i`, so once a run has *started* the Mac
won't idle-sleep midway through a render. It cannot wake a sleeping Mac to
begin with.

## Troubleshooting

**Nothing runs at the scheduled time**
`launchctl list | grep miscellaneousken`. Missing → re-run the installer.
Present but nothing in `logs/agent.log` → look at `logs/launchd.err.log`,
which catches failures happening before the wrapper starts.

**"ffmpeg not found" although it works in Terminal**
The classic launchd PATH problem. `run.sh` prepends `/opt/homebrew/bin` and
`/usr/local/bin`; if your ffmpeg is elsewhere, `which ffmpeg` and add that
directory to the `export PATH=` line.

**Alerts never arrive**
`--notify-test` first. Silence usually means the bot has never been messaged
by you, or `TELEGRAM_BOT_TOKEN` is wrong. `notify()` deliberately never raises,
so a broken alert can't take down a run — which also means it fails quietly.

**Runs succeed but post nothing**
Normal when everything fresh has already been posted. `"empty"` is in
`notify.on`, so you'll get told rather than left guessing.
