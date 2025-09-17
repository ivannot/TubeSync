#!/bin/sh
# Restart TubeSync Watcher on Synology (with Log Center messages)

CONFIG="/volume2/TubeSync/config.ini"
VENV="/volume2/TubeSync/.venv"
SCRIPT="/volume2/TubeSync/tubesync_watcher.py"
EVENT_HEX="${TS_EVENT_HEX:-0x11100000}"   # opzionale: export TS_EVENT_HEX=0x11100001

synolog() {
  # Usage: synolog <level> "<message>"
  LEVEL="$1"
  MSG="$2"
  if [ -x /usr/syno/bin/synologset1 ]; then
    /usr/syno/bin/synologset1 sys "$LEVEL" "$EVENT_HEX" "TubeSync Restart: $MSG"
  else
    logger -t "TubeSyncRestart" -p user."$LEVEL" "$MSG"
  fi
}

echo "👉 Stopping any running TubeSync Watcher..."
synolog info "Stopping watcher (pkill -f $SCRIPT)"
pkill -f "$SCRIPT" >/dev/null 2>&1 || true
sleep 2

echo "👉 Starting TubeSync Watcher..."
if [ -f "$VENV/bin/activate" ]; then
  # shellcheck disable=SC1090
  . "$VENV/bin/activate"
else
  synolog err "Virtualenv not found at $VENV"
  echo "❌ Virtualenv not found at $VENV"
  exit 1
fi

if [ ! -f "$SCRIPT" ]; then
  synolog err "Watcher script not found at $SCRIPT"
  echo "❌ Watcher script not found at $SCRIPT"
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  synolog err "Config not found at $CONFIG"
  echo "❌ Config not found at $CONFIG"
  exit 1
fi

nohup python3 "$SCRIPT" "$CONFIG" >/dev/null 2>&1 &
PID=$!

if [ -n "$PID" ]; then
  synolog info "Watcher started (pid=$PID)"
  echo "✅ TubeSync Watcher restarted (pid=$PID)."
else
  synolog err "Watcher failed to start"
  echo "❌ Failed to start TubeSync Watcher."
  exit 1
fi
