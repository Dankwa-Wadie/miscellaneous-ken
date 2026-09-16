#!/usr/bin/env bash
# run.sh — what launchd actually executes.
#
# A wrapper exists for three reasons, each of which is a real failure otherwise:
#
#   1. launchd gives a job a minimal PATH (roughly /usr/bin:/bin:/usr/sbin:/sbin).
#      Homebrew's ffmpeg lives in /opt/homebrew/bin, so the agent would render
#      nothing and report "ffmpeg not found" despite ffmpeg working in Terminal.
#   2. launchd has no EnvironmentFile. Secrets would otherwise sit in the plist
#      in ~/Library/LaunchAgents, which is more exposed than one 0600 file.
#   3. caffeinate keeps the Mac awake for the duration of a run, so a render
#      isn't suspended halfway by idle sleep.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# Homebrew first: Apple Silicon, then Intel, then the usual suspects.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Secrets. set -a exports everything defined in the file.
if [ -f mken.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./mken.env
  set +a
fi

mkdir -p logs work out
LOG="$PROJECT_DIR/logs/agent.log"

# Keep the log from growing forever — this runs three times a day for years.
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 5000000 ]; then
  mv "$LOG" "$LOG.1"
fi

{
  echo
  echo "================================================================"
  echo "run started $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "================================================================"
} >>"$LOG"

# caffeinate -i prevents idle sleep while the command runs, and exits with it.
CAFFEINATE=""
command -v caffeinate >/dev/null 2>&1 && CAFFEINATE="caffeinate -i"

# shellcheck disable=SC2086
$CAFFEINATE "$PROJECT_DIR/.venv/bin/python3" "$PROJECT_DIR/agent.py" -v "$@" >>"$LOG" 2>&1
rc=$?

echo "run finished rc=$rc at $(date '+%H:%M:%S')" >>"$LOG"
exit $rc
