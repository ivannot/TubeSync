#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TubeSync Watcher — Log nel registro System (DSM)
- Monitora SOLO le cartelle in `source_dirs` (ricorsivo)
- Filtra eventi SOLO per le estensioni in `allowed_extensions`
- Debounce/settle/max_debounce/rescan/pause
- Lancia:
  /volume2/TubeSync/.venv/bin/python /volume2/TubeSync/tubesync_synology.py /volume2/TubeSync/config.ini
"""

import sys, time, threading, subprocess, logging, os, subprocess as sp
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

DEFAULT_DEBOUNCE_SECONDS = 90
PAUSE_FILE = Path("/volume2/TubeSync/.auth_paused")

# --- Log Center (System) ---
class SynologySystemLogHandler(logging.Handler):
    LEVEL_MAP = {logging.DEBUG:"info", logging.INFO:"info", logging.WARNING:"warn", logging.ERROR:"err", logging.CRITICAL:"crit"}
    def emit(self, record):
        try:
            msg = self.format(record)
            level = self.LEVEL_MAP.get(record.levelno, "info")
            sp.run(["/usr/syno/bin/synologset1","sys",level,f"TubeSync: {msg}"],
                   check=False, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        except Exception:
            pass

for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)
fmt = logging.Formatter("%(message)s")
syno = SynologySystemLogHandler(); syno.setFormatter(fmt)
console = logging.StreamHandler(); console.setFormatter(fmt)   # utile in foreground
logging.basicConfig(level=logging.INFO, handlers=[syno, console])
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
        self._timer = None; self._lock = threading.Lock()
        self._first_event_ts = None; self._last_event_ts = None
        threading.Thread(target=self._rescan_loop, daemon=True).start()
        threading.Thread(target=self._activity_loop, daemon=True).start()

    def trigger(self):
        now = time.time()
        with self._lock:
            if self._first_event_ts is None: self._first_event_ts = now
            self._last_event_ts = now
            if self._timer: self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._maybe_run)
            self._timer.daemon = True; self._timer.start()

    def _maybe_run(self):
        with self._lock:
            if self._last_event_ts is None: return
            since_last = time.time() - self._last_event_ts
            total_wait = time.time() - (self._first_event_ts or time.time())
            if since_last >= self.settle or total_wait >= self.max_debounce:
                self._run(); self._first_event_ts = None; self._last_event_ts = None
            else:
                self._timer = threading.Timer(self.debounce, self._maybe_run)
                self._timer.daemon = True; self._timer.start()

    def _run(self):
        if PAUSE_FILE.exists():
            logger.info(f"Pausa attiva ({PAUSE_FILE}). Skip esecuzione uploader.")
            return
        logger.info(f"Esecuzione: {' '.join(self.cmd)}")
        try:
            proc = subprocess.Popen(self.cmd); code = proc.wait()
            logger.info(f"Uploader terminato con exit code {code}")
        except Exception as e:
            logger.exception(f"Errore nell'esecuzione: {e}")

    def _rescan_loop(self):
        while True:
            mins = max(1, int(self.rescan_minutes or 60))
            for _ in range(mins * 6):
                time.sleep(self.pause_check_seconds if PAUSE_FILE.exists() else 10)
            logger.info("Rescan periodico: trigger massivo.")
            self._run()

    def _activity_loop(self):
        while True:
            time.sleep(max(30, int(self.event_log_interval_seconds or 180)))
            if self._last_event_ts:
                ago = int(time.time() - self._last_event_ts)
                logger.info(f"Eventi recenti: ultimo evento {ago}s fa; debounce={self.debounce}s, settle={self.settle}s.")

class Handler(PatternMatchingEventHandler):
    def __init__(self, runner, patterns):
        super().__init__(patterns=patterns, ignore_directories=True); self.runner = runner
    def on_created(self, e): logger.info(f"created: {e.src_path}"); self.runner.trigger()
    def on_moved(self, e):   logger.info(f"moved: {e.src_path} -> {e.dest_path}"); self.runner.trigger()
    def on_modified(self, e):logger.info(f"modified: {e.src_path}"); self.runner.trigger()

def read_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists(): print(f"Config non trovato: {cfg_path}", file=sys.stderr); sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(";", "#")); cfg.read(cfg_path); return cfg

def build_patterns_from_extensions(ext_list):
    pats = []
    for e in ext_list:
        e = e.strip(); 
        if not e: continue
        if not e.startswith("."): e = "."+e
        base = e.lower()
        pats += [f"*{base}", f"*{base.upper()}"]
        if base != base.capitalize(): pats.append(f"*{base.capitalize()}")
    return sorted(set(pats))

def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv)>1 else Path("/volume2/TubeSync/config.ini")
    cfg = read_config(cfg_path)

    if not cfg.has_option("general", "source_dirs"):
        logger.error("source_dirs mancante in [general]"); sys.exit(2)
    roots = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]

    if not cfg.has_option("general", "allowed_extensions"):
        logger.error("allowed_extensions mancante in [general]"); sys.exit(2)
    exts = [e.strip() for e in cfg.get("general", "allowed_extensions").split(",") if e.strip()]
    patterns = build_patterns_from_extensions(exts)
    logger.info(f"Estensioni monitorate: {patterns}")

    debounce_seconds = cfg.getint("watcher","debounce_seconds",fallback=90)
    settle_seconds = cfg.getint("watcher","settle_seconds",fallback=60)
    max_debounce_seconds = cfg.getint("watcher","max_debounce_seconds",fallback=900)
    rescan_minutes = cfg.getint("watcher","rescan_minutes",fallback=60)
    pause_check_seconds = cfg.getint("watcher","pause_check_seconds",fallback=30)
    event_log_interval_seconds = cfg.getint("watcher","event_log_interval_seconds",fallback=180)

    cmd = ["/volume2/TubeSync/.venv/bin/python","/volume2/TubeSync/tubesync_synology.py", str(cfg_path)]
    runner = DebouncedRunner(cmd, debounce_seconds, settle_seconds, max_debounce_seconds,
                             rescan_minutes, pause_check_seconds, event_log_interval_seconds)

    if PAUSE_FILE.exists():
        logger.info(f"Pausa attiva ({PAUSE_FILE}). Skip scansione iniziale.")
    else:
        logger.info("Avvio watcher: scansione iniziale.")
        runner._run()

    from watchdog.observers import Observer
    observer = Observer()
    any_root = False
    for root in roots:
        p = Path(root).expanduser()
        if not p.exists():
            logger.warning(f"Cartella NON trovata: {p}"); continue
        any_root = True; logger.info(f"Osservo: {p}")
        observer.schedule(Handler(runner, patterns), str(p), recursive=True)

    if not any_root:
        logger.error("Nessuna root valida da osservare. Esco."); sys.exit(3)

    observer.start(); logger.info("TubeSync Watcher attivo. In attesa di eventi...")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stop watcher (KeyboardInterrupt)"); observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
