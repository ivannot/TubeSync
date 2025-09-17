#!/bin/sh
# Start TubeSync Watcher on Synology

CONFIG="/volume2/TubeSync/config.ini"
VENV="/volume2/TubeSync/.venv"
SCRIPT="/volume2/TubeSync/tubesync_watcher.py"
EVENT_HEX="${TS_EVENT_HEX:-0x11100000}"

synolog() {
  LEVEL="$1"
  MSG="$2"
  if [ -x /usr/syno/bin/synologset1 ]; then
    /usr/syno/bin/synologset1 sys "$LEVEL" "$EVENT_HEX" "TubeSync Start: $MSG"
  else
    logger -t "TubeSyncStart" -p user."$LEVEL" "$MSG"
  fi
}

if [ ! -f "$VENV/bin/activate" ]; then
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

echo "👉 Starting TubeSync Watcher..."
. "$VENV/bin/activate"
nohup python3 "$SCRIPT" "$CONFIG" >/dev/null 2>&1 &
PID=$!

if [ -n "$PID" ]; then
  synolog info "Watcher started (pid=$PID)"
  echo "✅ TubeSync Watcher started (pid=$PID)."
else
  synolog err "Watcher failed to start"
  echo "❌ Failed to start TubeSync Watcher."
  exit 1
fi
