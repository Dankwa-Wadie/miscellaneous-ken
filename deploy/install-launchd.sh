#!/usr/bin/env bash
# install-launchd.sh — schedule the agent on this Mac. Idempotent.
#
#     cd ~/projects/miscellaneous-ken && bash deploy/install-launchd.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.miscellaneousken.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cd "$PROJECT_DIR"

echo "==> Project: $PROJECT_DIR"

# --- preflight: fail loudly now rather than silently at 09:00 -------------
fail=0
note() { echo "  !! $1"; fail=1; }

[ -d .venv ] || note "no .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
command -v ffmpeg >/dev/null 2>&1 || note "ffmpeg not on PATH — run: brew install ffmpeg"
[ -f token.json ] || note "token.json missing — run: python3 youtube_auth.py"
[ -f config.json ] || note "config.json missing"

if [ ! -f mken.env ]; then
  cp deploy/mken.env.example mken.env
  chmod 600 mken.env
  note "created mken.env from the example — edit it and add ANTHROPIC_API_KEY + TELEGRAM_BOT_TOKEN"
else
  chmod 600 mken.env
  grep -q 'REPLACE_ME' mken.env && note "mken.env still contains REPLACE_ME placeholders"
fi

[ "$fail" = "1" ] && { echo; echo "Fix the above, then re-run."; exit 1; }
echo "==> Preflight OK"

chmod +x deploy/run.sh
mkdir -p logs work out

# --- install -------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"

# Unload an existing copy first, or launchd keeps running the old definition.
# bootout is the modern spelling; the older `unload` is the fallback.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
  launchctl unload "$PLIST" 2>/dev/null || true

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    deploy/com.miscellaneousken.agent.plist > "$PLIST"
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || \
  launchctl load -w "$PLIST"

echo "==> Installed and loaded: $PLIST"
echo
launchctl list | grep "$LABEL" || echo "  (not listed yet — check the log paths below)"

cat <<EOF

Scheduled: 09:00, 14:00, 19:00 local time.

Verify, cheapest first:
  .venv/bin/python3 agent.py --notify-test
  .venv/bin/python3 agent.py --check-sources
  bash deploy/run.sh --dry-run --limit 1     # exactly what launchd will run
  tail -f logs/agent.log

Trigger a real run right now:
  launchctl kickstart -k gui/$(id -u)/$LABEL

Control:
  launchctl bootout gui/$(id -u)/$LABEL      # stop scheduling
  bash deploy/install-launchd.sh             # start again

Sleep: a MacBook with the lid shut will not run these. Either keep it awake
(System Settings > Lock Screen, and Battery > Options > "Prevent automatic
sleeping when the display is off" while on power), or accept that a missed
slot runs shortly after the Mac wakes.
EOF
