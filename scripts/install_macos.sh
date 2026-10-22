#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${1:-$HOME/alpr-gate}"
PYTHON="${PYTHON:-/opt/homebrew/bin/python3.11}"
LOG_DIR="$HOME/Library/Logs/alpr-gate"
PLIST="$HOME/Library/LaunchAgents/com.local.alpr-gate.plist"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python not found: $PYTHON"
  echo "Install Python 3.10-3.13 or set PYTHON=/path/to/python3.12"
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
  echo "ffmpeg not found; install it with: brew install ffmpeg"
  exit 1
fi

mkdir -p "$APP_DIR" "$LOG_DIR" "$(dirname "$PLIST")"
cp "$REPO_DIR/alpr_gate.py" "$REPO_DIR/dashboard.html" "$REPO_DIR/requirements.txt" "$APP_DIR/"

NEEDS_CONFIG=0
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cp "$REPO_DIR/config.example.yaml" "$APP_DIR/config.yaml"
  NEEDS_CONFIG=1
fi
chmod 600 "$APP_DIR/config.yaml"

if [ ! -d "$APP_DIR/venv" ]; then
  "$PYTHON" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/python" -m pip install -q -U pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

"$PYTHON" - "$REPO_DIR/deploy/com.local.alpr-gate.plist.template" "$PLIST" "$APP_DIR" "$LOG_DIR" <<'PY'
import sys
from pathlib import Path
src, dst, app_dir, log_dir = sys.argv[1:]
text = Path(src).read_text()
text = text.replace("__APP_DIR__", app_dir).replace("__LOG_DIR__", log_dir)
Path(dst).write_text(text)
PY

echo "Installed service files in $APP_DIR"
echo "Configuration: $APP_DIR/config.yaml"
echo "Logs: $LOG_DIR"

if [ "$NEEDS_CONFIG" -eq 1 ]; then
  echo
  echo "Edit config.yaml, then load the service with:"
  echo "  launchctl bootstrap gui/$(id -u) $PLIST"
  echo "  launchctl enable gui/$(id -u) com.local.alpr-gate"
  exit 0
fi

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.local.alpr-gate"
echo "Service loaded. Check: curl -s http://127.0.0.1:8099/status"
