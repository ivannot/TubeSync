#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, logging, logging.handlers, subprocess, threading
from pathlib import Path
from configparser import ConfigParser
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------- Logging su Synology + console opzionale ----------
class SynologyLogHandler(logging.Handler):
    LEVEL_MAP = {
        logging.CRITICAL: "err",
        logging.ERROR:    "err",
        logging.WARNING:  "warn",
        logging.INFO:     "info",
        logging.DEBUG:    "debug",
    }
    def __init__(self, program="TubeSyncWatcher"):
        super().__init__()
        self.program = program
        self.event_hex = os.getenv("TS_EVENT_HEX", "0x11100000")

    def emit(self, record):
        try:
            level = self.LEVEL_MAP.get(record.levelno, "info")
            msg = self.format(record)
            full = f"{self.program}: {msg}"
            subprocess.run(
                ["/usr/syno/bin/synologset1", "sys", level, self.event_hex, full],
                check=False
            )
        except Exception:
            pass

def setup_logging():
    handlers = [SynologyLogHandler(program="TubeSyncWatcher")]
    try:
        if os.path.exists("/dev/log"):
            handlers.append(logging.handlers.SysLogHandler(address="/dev/log"))
    except Exception:
        pass
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncWatcher[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# ---------- Config ----------
def load_config(cfg_path: Path) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=('#', ';'))
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(2)
    cfg.read(cfg_path)
    return cfg

def cfg_paths(cfg: ConfigParser):
    # percorsi relativi risolti rispetto alla cartella dello script
    def getp(section, option, default_rel) -> Path:
        if cfg.has_option(section, option):
            return Path(cfg.get(section, option)).expanduser().resolve()
        return (SCRIPT_DIR / default_rel).resolve()
    return {
        "client_secret": getp("general", "client_secret_path", "client_secret.json"),
        "token":         getp("general", "token_path", "token.json"),
        "db":            getp("general", "db_path", "state.db"),
        "log":           getp("general", "log_path", "tubesync.log"),
        "pause":         getp("general", "pause_file", ".pause_until"),
    }

# ---------- Utility ----------
def parse_allowed_exts(cfg: ConfigParser):
    raw = cfg.get("general", "allowed_extensions", fallback=".mp4,.mov,.m4v,.avi,.mkv")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}

def is_allowed_ext(path: Path, allowed_exts: set) -> bool:
    return path.suffix.lower() in allowed_exts

def run_uploader(cfg_path: Path):
    # usa lo stesso interprete del watcher
    python = sys.executable
    cmd = [str(python), str(SCRIPT_DIR / "tubesync_synology.py"), str(cfg_path)]
    logging.info(f"Esecuzione comando: {' '.join(cmd)}")
    try:
        rc = subprocess.call(cmd)
        if rc != 0:
            logging.error(f"Uploader terminato con exit code {rc}")
        else:
            logging.info("Uploader completato.")
    except Exception as e:
        logging.error(f"Impossibile eseguire uploader: {e}")

def pause_active(pause_file: Path) -> Optional[float]:
    if not pause_file.exists():
        return None
    try:
        until = float(pause_file.read_text().strip())
    except Exception:
        return None
    if time.time() < until:
        return until
    # scaduta: rimuovi
    try:
        pause_file.unlink()
    except Exception:
        pass
    return None

# ---------- Watcher ----------
class DebouncedHandler(FileSystemEventHandler):
    def __init__(self, cfg: ConfigParser, cfg_path: Path):
        super().__init__()
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.allowed_exts = parse_allowed_exts(cfg)

        self.debounce_seconds = cfg.getint("watcher", "debounce_seconds", fallback=300)
        self.settle_seconds = cfg.getint("watcher", "settle_seconds", fallback=60)
        self.max_debounce_seconds = cfg.getint("watcher", "max_debounce_seconds", fallback=900)
        self.pause_check_seconds = cfg.getint("watcher", "pause_check_seconds", fallback=30)

        self._timer: Optional[threading.Timer] = None
        self._first_event_ts: Optional[float] = None
        self._last_touch: dict[str, float] = {}

        self._lock = threading.Lock()
        self._stop = False

        self._pause_file = cfg_paths(cfg)["pause"]

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if not is_allowed_ext(p, self.allowed_exts):
            return

        with self._lock:
            now = time.time()
            self._last_touch[str(p)] = now
            if self._first_event_ts is None:
                self._first_event_ts = now
            logging.info(f"Evento FS → debounce {self.debounce_seconds}s (reason=filesystem).")
            self._schedule()

    def _schedule(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_seconds, self._deferred_trigger)
        self._timer.daemon = True
        self._timer.start()

    def _all_settled(self) -> bool:
        if self.settle_seconds <= 0:
            return True
        now = time.time()
        # tutti i file visti devono essere “fermi” da almeno settle_seconds
        for _p, ts in self._last_touch.items():
            if now - ts < self.settle_seconds:
                return False
        return True

    def _deferred_trigger(self):
        with self._lock:
            if self._stop:
                return
            # quota in corso?
            paused_until = pause_active(self._pause_file)
            if paused_until:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(paused_until))
                logging.info(f"In pausa fino a {when} — rimando trigger.")
                self._schedule()
                return

            now = time.time()
            # forza se si supera il max debounce
            if self._first_event_ts and (now - self._first_event_ts >= self.max_debounce_seconds):
                self._fire("max_debounce")
                return

            # aspetta che i file si stabilizzino
            if not self._all_settled():
                self._schedule()
                return

            self._fire("filesystem")

    def _fire(self, reason: str):
        self._timer = None
        self._first_event_ts = None
        self._last_touch.clear()
        logging.info(f"Trigger '{reason}': lancio uploader.")
        run_uploader(self.cfg_path)

    def stop(self):
        with self._lock:
            self._stop = True
            if self._timer:
                self._timer.cancel()
                self._timer = None

def main():
    setup_logging()

    cfg_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini").resolve()
    cfg = load_config(cfg_path)

    # sorgenti
    if not cfg.has_option("general", "source_dirs"):
        logging.error("Nessuna root valida da osservare. Esco..")
        sys.exit(2)
    roots = [Path(s.strip()).expanduser().resolve() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]
    roots = [r for r in roots if r.exists()]
    if not roots:
        logging.error("Nessuna root valida da osservare. Esco..")
        sys.exit(2)

    handler = DebouncedHandler(cfg, cfg_path)

    # se la pausa è SCADUTA all'avvio, rimuovi file e lancia subito un massivo
    pause_file = cfg_paths(cfg)["pause"]
    if pause_file.exists():
        paused_until = pause_active(pause_file)
        if paused_until is None and not pause_file.exists():
            logging.info("Pausa scaduta rilevata all'avvio: file rimosso, lancio run iniziale.")
            run_uploader(cfg_path)

    # avvia observer
    obs = Observer()
    for r in roots:
        logging.info(f"Osservo: {r}")
        obs.schedule(handler, str(r), recursive=True)
    obs.start()

    # rescan periodico
    rescan_minutes = cfg.getint("watcher", "rescan_minutes", fallback=60)

    try:
        last_rescan = 0.0
        while True:
            time.sleep(1)
            if rescan_minutes > 0:
                now = time.time()
                if now - last_rescan >= rescan_minutes * 60:
                    # se non in pausa, lancia run massivo
                    if not pause_active(pause_file):
                        logging.info("Trigger 'periodic_rescan': lancio uploader.")
                        run_uploader(cfg_path)
                    last_rescan = now
    except KeyboardInterrupt:
        pass
    finally:
        handler.stop()
        obs.stop()
        obs.join(timeout=5)

if __name__ == "__main__":
    main()
