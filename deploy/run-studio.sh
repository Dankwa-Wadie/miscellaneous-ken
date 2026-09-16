#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
if [ -f mken.env ]; then
  set -a
  . ./mken.env
  set +a
fi
exec "$PROJECT_DIR/.venv/bin/python3" -u "$PROJECT_DIR/studio_server.py"
