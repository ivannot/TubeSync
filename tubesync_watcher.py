#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, time, threading, subprocess, logging, logging.handlers, os
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

# ========= Log Center helper (metodo testato e funzionante) =========
def syno_log_info(tag: str, msg: str):
    """Scrive su Log Center usando synologset1 con ID generico 0x11100000"""
    try:
        subprocess.run([
            "synologset1", "sys", "info", "0x11100000",
            f"[TubeSync:{tag}] {msg} - {time.strftime('%F %T')}"
        ], check=False, timeout=2)
    except Exception:
        pass

def syno_log_warn(tag: str, msg: str):
    try:
        subprocess.run([
            "synologset1", "sys", "warn", "0x11100000",
            f"[TubeSync:{tag}] {msg} - {time.strftime('%F %T')}"
        ], check=False, timeout=2)
    except Exception:
        pass

def syno_log_err(tag: str, msg: str):
    try:
        subprocess.run([
            "synologset1", "sys", "err", "0x11100000",
            f"[TubeSync:{tag}] {msg} - {time.strftime('%F %T')}"
        ], check=False, timeout=2)
    except Exception:
        pass

# ========= Logging console (anche syslog) =========
def setup_logging():
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    fmt = logging.Formatter("%(name)s[%(process)d]: %(message)s")
    handlers = [logging.StreamHandler()]
    handlers[0].setFormatter(fmt)
    
    # Prova a configurare SysLogHandler
    try:
        if os.path.exists("/dev/log"):
            h = logging.handlers.SysLogHandler(
                address="/dev/log",
                facility=logging.handlers.SysLogHandler.LOG_USER
            )
            h.setFormatter(fmt)
            handlers.append(h)
    except Exception:
        pass
    
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    return logging.getLogger("TubeSync")

# ========= Runner con debounce =========
class DebouncedRunner:
    def __init__(self, logger, cmd, debounce_seconds, settle_seconds, max_debounce_seconds,
                 rescan_minutes, event_log_interval_seconds):
        self.log = logger
        self.cmd = cmd
        self.debounce = debounce_seconds
        self.settle = settle_seconds
        self.max_debounce = max_debounce_seconds
        self.rescan_minutes = rescan_minutes
        self.event_log_interval_seconds = event_log_interval_seconds
        self._timer = None
        self._first_event_ts = None
        self._last_event_ts = None
        self._lock = threading.Lock()
        threading.Thread(target=self._rescan_loop, daemon=True).start()
        threading.Thread(target=self._activity_loop, daemon=True).start()

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
                self._timer = threading.Timer(self.debounce, self._maybe_run)
                self._timer.daemon = True
                self._timer.start()

    def _run(self):
        msg = f"Esecuzione uploader: {' '.join(self.cmd)}"
        self.log.info(msg)
        syno_log_info("UPLOAD", msg)
        try:
            proc = subprocess.Popen(self.cmd)
            code = proc.wait()
            msg2 = f"Uploader terminato con exit code {code}"
            self.log.info(msg2)
            syno_log_info("UPLOAD", msg2)
        except Exception as e:
            self.log.exception(f"Errore esecuzione uploader: {e}")
            syno_log_err("UPLOAD_ERR", f"Errore esecuzione uploader: {e}")

    def _rescan_loop(self):
        while True:
            mins = max(1, int(self.rescan_minutes or 60))
            time.sleep(mins * 60)
            self.log.info("Rescan periodico: trigger massivo.")
            syno_log_info("RESCAN", "Rescan periodico: trigger massivo")
            self._run()

    def _activity_loop(self):
        while True:
            time.sleep(max(30, int(self.event_log_interval_seconds or 180)))
            if self._last_event_ts:
                ago = int(time.time() - self._last_event_ts)
                msg = f"Eventi recenti: ultimo {ago}s fa; debounce={self.debounce}s, settle={self.settle}s"
                self.log.info(msg)
                syno_log_info("ACTIVITY", msg)

# ========= Watchdog handler =========
class Handler(PatternMatchingEventHandler):
    def __init__(self, logger, runner, patterns):
        super().__init__(patterns=patterns, ignore_directories=True)
        self.log = logger
        self.runner = runner
    def on_created(self, e): self._log("created", e.src_path); self.runner.trigger()
    def on_moved(self, e):   self._log("moved", f"{e.src_path} -> {e.dest_path}"); self.runner.trigger()
    def on_modified(self, e):self._log("modified", e.src_path); self.runner.trigger()
    def _log(self, typ, path):
        msg = f"{typ}: {path}"
        self.log.info(msg)
        syno_log_info("EVENT", f"{typ}: {Path(path).name}")

# ========= Config & patterns =========
def read_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(";", "#"))
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
        patterns += [f"*{base}", f"*{base.upper()}"]
        cap = base.capitalize()
        if cap != base:
            patterns.append(f"*{cap}")
    return sorted(set(patterns))

# ========= Main =========
def main():
    logger = setup_logging()
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("/volume2/TubeSync/config.ini")
    cfg = read_config(cfg_path)

    if not cfg.has_option("general", "source_dirs"):
        logger.error("source_dirs mancante in [general]")
        sys.exit(2)
    roots = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]

    if not cfg.has_option("general", "allowed_extensions"):
        logger.error("allowed_extensions mancante in [general]")
        sys.exit(2)
    exts = [e.strip() for e in cfg.get("general", "allowed_extensions").split(",") if e.strip()]
    patterns = build_patterns_from_extensions(exts)
    logger.info(f"Estensioni monitorate: {patterns}")
    syno_log_info("START", f"Watcher avviato. Estensioni: {', '.join(patterns)}")

    debounce_seconds = cfg.getint("watcher", "debounce_seconds", fallback=90)
    settle_seconds = cfg.getint("watcher", "settle_seconds", fallback=60)
    max_debounce_seconds = cfg.getint("watcher", "max_debounce_seconds", fallback=900)
    rescan_minutes = cfg.getint("watcher", "rescan_minutes", fallback=60)
    event_log_interval_seconds = cfg.getint("watcher", "event_log_interval_seconds", fallback=180)

    cmd = ["/volume2/TubeSync/.venv/bin/python3", "/volume2/TubeSync/tubesync_synology.py", str(cfg_path)]

    logger.info("Avvio watcher: scansione iniziale.")
    syno_log_info("INIT", "Scansione iniziale: lancio uploader")
    runner = DebouncedRunner(logger, cmd, debounce_seconds, settle_seconds, max_debounce_seconds,
                             rescan_minutes, event_log_interval_seconds)
    runner._run()

    observer = Observer()
    any_root = False
    for root in roots:
        p = Path(root).expanduser()
        if not p.exists():
            w = f"Cartella NON trovata: {p}"
            logger.warning(w)
            syno_log_warn("WARN", w)
            continue
        any_root = True
        logger.info(f"Osservo: {p}")
        syno_log_info("WATCH", f"Osservo: {p}")
        observer.schedule(Handler(logger, runner, patterns), str(p), recursive=True)

    if not any_root:
        e = "Nessuna root valida da osservare. Esco."
        logger.error(e)
        syno_log_err("FATAL", e)
        sys.exit(3)

    observer.start()
    logger.info("TubeSync Watcher attivo. In attesa di eventi...")
    syno_log_info("RUNNING", "TubeSync Watcher attivo. In attesa di eventi")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stop watcher (KeyboardInterrupt)")
        syno_log_info("STOP", "Stop watcher (KeyboardInterrupt)")
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
