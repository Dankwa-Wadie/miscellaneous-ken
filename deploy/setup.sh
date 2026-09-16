#!/usr/bin/env bash
# setup.sh — prepare an Ubuntu box (Oracle Always Free ARM works well) to run
# the Miscellaneous Ken agent on a schedule.
#
# Run it ON THE SERVER, from the project directory:
#     cd ~/miscellaneous-ken && bash deploy/setup.sh
#
# Idempotent: safe to re-run after pulling changes.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> Project: $PROJECT_DIR"

# --- system packages ------------------------------------------------------
# ffmpeg is the only heavy dependency. python3-venv is separate from python3
# on Debian/Ubuntu and its absence is a classic first-deploy failure.
echo "==> Installing system packages (needs sudo)…"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip ffmpeg ca-certificates tzdata

echo "==> ffmpeg: $(ffmpeg -version | head -1)"

# --- python environment ---------------------------------------------------
if [ ! -d .venv ]; then
  echo "==> Creating virtualenv…"
  python3 -m venv .venv
fi
echo "==> Installing Python dependencies…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

# yt-dlp ages badly — YouTube changes break older versions, and the symptom
# is a confusing extraction error rather than anything that says "upgrade me".
./.venv/bin/pip install --quiet --upgrade yt-dlp

echo "==> yt-dlp: $(./.venv/bin/yt-dlp --version)"

mkdir -p work out assets

# --- secrets --------------------------------------------------------------
if [ ! -f mken.env ]; then
  cp deploy/mken.env.example mken.env
  chmod 600 mken.env
  echo
  echo "!! Created mken.env from the example. EDIT IT NOW and fill in:"
  echo "     ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN"
  echo "   Then re-run this script."
  echo
  exit 1
fi
chmod 600 mken.env
echo "==> mken.env present (permissions tightened to 600)"

for f in client_secret.json token.json; do
  if [ ! -f "$f" ]; then
    echo "!! MISSING $f — copy it from your Mac:"
    echo "     scp $f ubuntu@<server-ip>:$PROJECT_DIR/"
    MISSING=1
  else
    chmod 600 "$f"
  fi
done
[ "${MISSING:-0}" = "1" ] && { echo; echo "Fix the above, then re-run."; exit 1; }

# --- systemd --------------------------------------------------------------
echo "==> Installing systemd units…"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g; s|__USER__|$(whoami)|g" \
    deploy/mken.service | sudo tee /etc/systemd/system/mken.service >/dev/null
sudo cp deploy/mken.timer /etc/systemd/system/mken.timer

sudo systemctl daemon-reload
sudo systemctl enable --now mken.timer

echo
echo "==> Done."
echo
systemctl list-timers mken.timer --no-pager || true
echo
echo "Next:"
echo "  ./.venv/bin/python3 agent.py --notify-test          # prove alerting works"
echo "  ./.venv/bin/python3 agent.py --check-sources        # prove discovery works"
echo "  ./.venv/bin/python3 agent.py --dry-run --limit 1 -v # full rehearsal, no upload"
echo "  sudo systemctl start mken.service                   # trigger a real run now"
echo "  journalctl -u mken.service -f                       # watch it"
