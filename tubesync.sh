#!/bin/sh
# Unified control script for TubeSync (start|stop|restart|status)
# Reads paths from config.ini

CONFIG="/volume2/TubeSync/config.ini"
EVENT_HEX="${TS_EVENT_HEX:-0x11100000}"

# helper: extract value from ini
cfg() {
  awk -F'=' -v key="$1" '
    $1 ~ ("^" key "[[:space:]]*$") { gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2 }
  ' "$CONFIG" | head -n1
}

VENV="$(cfg venv_path)"
SCRIPT="$(cfg watcher_path)"
if [ -z "$VENV" ]; then VENV="/volume2/TubeSync/.venv"; fi
if [ -z "$SCRIPT" ]; then SCRIPT="/volume2/TubeSync/tubesync_watcher.py"; fi

synolog() {
  LEVEL="$1"; MSG="$2"
  if [ -x /usr/syno/bin/synologset1 ]; then
    /usr/syno/bin/synologset1 sys "$LEVEL" "$EVENT_HEX" "TubeSync: $MSG"
  else
    logger -t "TubeSync" -p user."$LEVEL" "$MSG"
  fi
}

start() {
  if [ ! -f "$VENV/bin/activate" ]; then
    echo "❌ Virtualenv not found at $VENV"
    synolog err "Start failed: venv missing"
    exit 1
  fi
  if [ ! -f "$SCRIPT" ]; then
    echo "❌ Watcher script not found at $SCRIPT"
    synolog err "Start failed: script missing"
    exit 1
  fi
  . "$VENV/bin/activate"
  nohup python3 "$SCRIPT" "$CONFIG" >/dev/null 2>&1 &
  PID=$!
  echo "✅ TubeSync started (pid=$PID)"
  synolog info "Started (pid=$PID)"
}

stop() {
  pkill -f "$SCRIPT" >/dev/null 2>&1 || true
  echo "🛑 TubeSync stopped (if running)"
  synolog info "Stopped"
}

status() {
  pgrep -af "$SCRIPT" || echo "❌ TubeSync not running"
}

restart() {
  stop
  sleep 2
  start
}

case "$1" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
