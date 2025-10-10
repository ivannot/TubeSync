#!/bin/sh
### TubeSync Watcher service script for Synology DSM
### Usage: ./tubesync.sh [start|stop|restart|status]

VENV="/volume2/TubeSync/.venv/bin/activate"
PYTHON="/volume2/TubeSync/.venv/bin/python3"
WATCHER="/volume2/TubeSync/tubesync_watcher.py"
CONFIG="/volume2/TubeSync/config.ini"
PIDFILE="/volume2/TubeSync/tubesync_watcher.pid"

# Funzione per loggare su Log Center (metodo testato e funzionante)
syno_log() {
    LEVEL="$1"
    MSG="$2"
    synologset1 sys "$LEVEL" 0x11100000 "[TubeSync:SCRIPT] $MSG - $(date '+%F %T')"
}

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "⚠️  TubeSync Watcher already running (pid=$(cat "$PIDFILE"))"
        exit 0
    fi

    echo "✅ Starting TubeSync Watcher..."
    /bin/sh -c "source \"$VENV\" && nohup \"$PYTHON\" \"$WATCHER\" \"$CONFIG\" >/dev/null 2>&1 & echo \$! > \"$PIDFILE\""

    syno_log "info" "Watcher started (pid=$(cat "$PIDFILE"))"
    echo "✅ TubeSync Watcher started (pid=$(cat "$PIDFILE"))"
}

stop() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        PID=$(cat "$PIDFILE")
        echo "🛑 Stopping TubeSync Watcher (pid=$PID)..."
        kill "$PID" 2>/dev/null
        rm -f "$PIDFILE"
        syno_log "info" "Watcher stopped (pid=$PID)"
        echo "🛑 TubeSync Watcher stopped."
    else
        echo "⚠️  TubeSync Watcher not running."
    fi
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "✅ TubeSync Watcher is running (pid=$(cat "$PIDFILE"))"
    else
        echo "⚠️  TubeSync Watcher is not running."
    fi
}

restart() {
    stop
    sleep 1
    start
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac
