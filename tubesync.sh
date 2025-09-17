#!/bin/sh
# TubeSync controller (start|stop|restart|status)
# Tutti i path sono relativi alla directory dello script.

set -eu

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$BASE_DIR/config.ini"
VENV="$BASE_DIR/.venv"
WATCHER="$BASE_DIR/tubesync_watcher.py"
EVENT_HEX="${TS_EVENT_HEX:-0x11100000}"   # opzionale: export TS_EVENT_HEX=0x11100001

synolog() {
  # synolog <level> "<message>"
  level="$1"; shift
  msg="$*"
  if [ -x /usr/syno/bin/synologset1 ]; then
    /usr/syno/bin/synologset1 sys "$level" "$EVENT_HEX" "TubeSync: $msg"
  else
    logger -t "TubeSync" -p user."$level" "$msg"
  fi
}

_require_files() {
  [ -f "$CONFIG" ]  || { echo "❌ Missing $CONFIG"; synolog err "Missing config.ini"; exit 1; }
  [ -f "$WATCHER" ] || { echo "❌ Missing $WATCHER"; synolog err "Missing tubesync_watcher.py"; exit 1; }
  [ -f "$VENV/bin/activate" ] || { echo "❌ Missing venv at $VENV"; synolog err "Missing venv"; exit 1; }
}

start() {
  _require_files
  # shellcheck disable=SC1090
  . "$VENV/bin/activate"
  nohup python3 "$WATCHER" "$CONFIG" >/dev/null 2>&1 &
  pid=$!
  echo "✅ TubeSync Watcher started (pid=$pid)"
  synolog info "Watcher started (pid=$pid)"
}

stop() {
  # killer “prudente”: solo il watcher di questa cartella
  pids="$(pgrep -f "$WATCHER" || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs -r kill >/dev/null 2>&1 || true
    sleep 1
    echo "🛑 TubeSync Watcher stopped."
    synolog info "Watcher stopped"
  else
    echo "⚠️  No TubeSync Watcher running."
    synolog warning "No watcher running"
  fi
}

status() {
  pgrep -af "$WATCHER" || echo "❌ TubeSync Watcher not running"
}

restart() {
  stop
  sleep 2
  start
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
