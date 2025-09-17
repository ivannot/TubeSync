#!/bin/sh
# Stop TubeSync Watcher on Synology

SCRIPT="/volume2/TubeSync/tubesync_watcher.py"
EVENT_HEX="${TS_EVENT_HEX:-0x11100000}"

synolog() {
  LEVEL="$1"
  MSG="$2"
  if [ -x /usr/syno/bin/synologset1 ]; then
    /usr/syno/bin/synologset1 sys "$LEVEL" "$EVENT_HEX" "TubeSync Stop: $MSG"
  else
    logger -t "TubeSyncStop" -p user."$LEVEL" "$MSG"
  fi
}

echo "👉 Stopping TubeSync Watcher..."
pkill -f "$SCRIPT" >/dev/null 2>&1 || true
RC=$?

if [ $RC -eq 0 ]; then
  synolog info "Watcher stopped"
  echo "✅ TubeSync Watcher stopped."
else
  synolog warning "No watcher running"
  echo "⚠️ No TubeSync Watcher was running."
fi
