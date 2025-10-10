#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TubeSync Watcher (Log Center)
- Monitora SOLO le cartelle in `source_dirs` (ricorsivo)
- Filtra eventi SOLO per le estensioni in `allowed_extensions`
- Parametri avanzati: debounce, settle, max_debounce, rescan periodico, pausa
- Lancia lo script principale:
    /volume2/TubeSync/.venv/bin/python /volume2/TubeSync/tubesync_synology.py /volume2/TubeSync/config.ini
"""

import sys, time, threading, subprocess, logging, logging.handlers, os, socket
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

DEFAULT_DEBOUNCE_SECONDS = 90
PAUSE_FILE = Path("/volume2/TubeSync/.auth_paused")

# --- Logging su Log Center (+ fallback) ---
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

handlers = []
syslog_socket = "/dev/log" if os.path.exists("/dev/log") else ("/run/log" if os.path.exists("/run/log") else None)
if syslog_socket:
    try:
        h = logging.handlers.SysLogHandler(address=syslog_socket, facility=logging.handlers.SysLogHandler.LOG_USER)
        handlers.append(h)
    except Exception:
        pass
if not handlers:
    try:
        h = logging.handlers.SysLogHandler(address=("127.0.0.1", 514),
                                           facility=logging.handlers.SysLogHandler.LOG_USER,
                                           socktype=socket.SOCK_DGRAM)
        handlers.append(h)
    except Exception:
        pass
if not handlers:
    handlers = [logging.StreamHandler()]

fmt = logging.Formatter("TubeSyncWatcher[%(process)d]: [%(levelname)s] %(message)s")
for h in handlers:
    h.setFormatter(fmt)
logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger("TubeSyncWatcher")

class DebouncedRunner:
    def __init__(self, cmd, debounce_seconds, settle_seconds, max_debounce_seconds,
                 rescan_minutes, pause_check_seconds, event_log_interval_seconds):
        self.cmd = cmd
        self.debounce = debounce_seconds
        self.settle = settle_seconds
        self.max_debounce = max_debounce_seconds
        self.rescan_minutes = rescan_minutes
        self.pause_check_seconds = pause_check_seconds
        self.event_log_interval_seconds = event_log_interval_seconds

        self._timer = None
        self._lock = threading.Lock()
        self._first_event_ts = None
        self._last_event_ts = None

        # thread per rescan periodico
        self._rescan_thread = threading.Thread(target=self._rescan_loop, daemon=True)
        self._rescan_thread.start()

        # thread per log attività
        self._activity_thread = threading.Thread(target=self._activity_loop, daemon=True)
        self._activity_thread.start()

    def trigger(self):
        now = time.time()
        with self._lock:
            if self._first_event_ts is None:
                self._first_event_ts = now
            self._last_event_ts = now

            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._maybe_run)
            self._timer.daemon = True
            self._timer.start()

    def _maybe_run(self):
        with self._lock:
            if self._last_event_ts is None:
                return
            since_last = time.time() - self._last_event_ts
            total_wait = time.time() - (self._first_event_ts or time.time())
            if since_last >= self.settle or total_wait >= self.max_debounce:
                self._run()
                self._first_event_ts = None
                self._last_event_ts = None
            else:
                # riprogramma finché non “si calma” o supera max_debounce
                self._timer = threading.Timer(self.debounce, self._maybe_run)
                self._timer.daemon = True
                self._timer.start()

    def _run(self):
        if PAUSE_FILE.exists():
            logger.info(f"Pausa attiva ({PAUSE_FILE}). Skip esecuzione uploader.")
            return
        logger.info(f"Esecuzione comando: {' '.join(self.cmd)}")
        try:
            proc = subprocess.Popen(self.cmd)
            code = proc.wait()
            logger.info(f"Comando terminato con exit code {code}")
        except Exception as e:
            logger.exception(f"Errore nell'esecuzione del comando: {e}")

    def _rescan_loop(self):
        # trigger massivo periodico
        while True:
            mins = max(1, int(self.rescan_minutes or 60))
            for _ in range(mins * 6):  # controlla anche la pausa senza aspettare minuti interi
                if PAUSE_FILE.exists():
                    time.sleep(self.pause_check_seconds)
                else:
                    time.sleep(10)
            logger.info("Rescan periodico: trigger upload massivo.")
            self._run()

    def _activity_loop(self):
        while True:
            time.sleep(max(30, int(self.event_log_interval_seconds or 180)))
            if self._last_event_ts:
                ago = int(time.time() - self._last_event_ts)
                logger.info(f"Eventi recenti: ultimo evento {ago}s fa; debounce={self.debounce}s, settle={self.settle}s.")

class Handler(PatternMatchingEventHandler):
    def __init__(self, runner, patterns):
        super().__init__(patterns=patterns, ignore_directories=True)
        self.runner = runner
    def on_created(self, event):
        logger.info(f"created: {event.src_path}")
        self.runner.trigger()
    def on_moved(self, event):
        logger.info(f"moved: {event.src_path} -> {event.dest_path}")
        self.runner.trigger()
    def on_modified(self, event):
        logger.info(f"modified: {event.src_path}")
        self.runner.trigger()

def read_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read(cfg_path)
    return cfg

def build_patterns_from_extensions(ext_list):
    patterns = []
    for e in ext_list:
        e = e.strip()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        base = e.lower()
        patterns.append(f"*{base}")
        patterns.append(f"*{base.upper()}")
        if base != base.capitalize():
            patterns.append(f"*{base.capitalize()}")
    return sorted(set(patterns))

def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("/volume2/TubeSync/config.ini")
    cfg = read_config(cfg_path)

    # source_dirs
    if not cfg.has_option("general", "source_dirs"):
        logger.error("source_dirs mancante in [general] del config.ini")
        sys.exit(2)
    roots = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]

    # allowed_extensions -> patterns
    if not cfg.has_option("general", "allowed_extensions"):
        logger.error("allowed_extensions mancante in [general] del config.ini")
        sys.exit(2)
    exts = [e.strip() for e in cfg.get("general", "allowed_extensions").split(",") if e.strip()]
    patterns = build_patterns_from_extensions(exts)
    logger.info(f"Estensioni monitorate: {patterns}")

    # watcher params
    debounce_seconds = cfg.getint("watcher", "debounce_seconds", fallback=DEFAULT_DEBOUNCE_SECONDS)
    settle_seconds = cfg.getint("watcher", "settle_seconds", fallback=60)
    max_debounce_seconds = cfg.getint("watcher", "max_debounce_seconds", fallback=900)
    rescan_minutes = cfg.getint("watcher", "rescan_minutes", fallback=60)
    pause_check_seconds = cfg.getint("watcher", "pause_check_seconds", fallback=30)
    event_log_interval_seconds = cfg.getint("watcher", "event_log_interval_seconds", fallback=180)

    # comando uploader
    cmd = ["/volume2/TubeSync/.venv/bin/python", "/volume2/TubeSync/tubesync_synology.py", str(cfg_path)]
    runner = DebouncedRunner(
        cmd,
        debounce_seconds=debounce_seconds,
        settle_seconds=settle_seconds,
        max_debounce_seconds=max_debounce_seconds,
        rescan_minutes=rescan_minutes,
        pause_check_seconds=pause_check_seconds,
        event_log_interval_seconds=event_log_interval_seconds,
    )

    # prima scansione subito
    if PAUSE_FILE.exists():
        logger.info(f"Pausa attiva ({PAUSE_FILE}). Skip scansione iniziale.")
    else:
        logger.info("Avvio watcher: eseguo subito una scansione iniziale.")
        runner._run()

    # osservatori
    observer = Observer()
    any_root = False
    for root in roots:
        p = Path(root).expanduser()
        if not p.exists():
            logger.warning(f"Cartella da osservare NON trovata: {p}")
            continue
        any_root = True
        logger.info(f"Osservo: {p}")
        observer.schedule(Handler(runner, patterns), str(p), recursive=True)

    if not any_root:
        logger.error("Nessuna root valida da osservare. Esco.")
        sys.exit(3)

    observer.start()
    logger.info("TubeSync Watcher attivo. In attesa di eventi...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stop watcher (KeyboardInterrupt)")
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
