#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, logging, logging.handlers, subprocess, threading
from pathlib import Path
from configparser import ConfigParser
from queue import Queue, Empty

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

SCRIPT_DIR = Path(__file__).resolve().parent

# -------------------------
# Logging (Synology first)
# -------------------------

class SynologyLogHandler(logging.Handler):
    LEVEL_MAP = {
        logging.CRITICAL: "crit",
        logging.ERROR:    "err",
        logging.WARNING:  "warning",
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
    if os.getenv("TS_FILE"):
        handlers.append(logging.FileHandler(os.getenv("TS_FILE")))
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncWatcher[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# -------------------------
# Config helpers
# -------------------------

def load_config(cfg_path: Path) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg.read(cfg_path)
    return cfg

def get_path_from_cfg(cfg: ConfigParser, section: str, option: str, default_rel: str) -> Path:
    if cfg.has_option(section, option):
        v = cfg.get(section, option)
        p = Path(v)
        if not p.is_absolute():
            p = (SCRIPT_DIR / v)
        return p.expanduser().resolve()
    return (SCRIPT_DIR / default_rel).resolve()

# -------------------------
# Pause helpers (quota)
# -------------------------

def read_pause_until(cfg: ConfigParser) -> float:
    pf = get_path_from_cfg(cfg, "general", "pause_file", ".pause_until")
    if not pf.exists():
        return 0.0
    try:
        return float(pf.read_text().strip())
    except Exception:
        try:
            pf.unlink(missing_ok=True)
        except Exception:
            pass
        return 0.0

def pause_active(cfg: ConfigParser) -> tuple[bool, float]:
    until = read_pause_until(cfg)
    return (time.time() < until, until)

# -------------------------
# Uploader runner
# -------------------------

def uploader_is_running(uploader_path: Path) -> bool:
    # Controllo semplice: c’è un processo python che sta eseguendo quel file?
    try:
        out = subprocess.check_output(["pgrep", "-af", str(uploader_path)], text=True)
        lines = [ln for ln in out.strip().splitlines() if "python" in ln or "python3" in ln]
        return len(lines) > 0
    except subprocess.CalledProcessError:
        return False

def run_uploader(cfg_path: Path, uploader_path: Path):
    if uploader_is_running(uploader_path):
        logging.info("Uploader già in esecuzione: skip.")
        return
    logging.info(f"Esecuzione comando: {sys.executable} {uploader_path} {cfg_path}")
    try:
        rc = subprocess.call([sys.executable, str(uploader_path), str(cfg_path)])
        logging.info(f"Comando terminato con exit code {rc}.")
    except Exception as e:
        logging.error(f"Impossibile eseguire uploader: {e}")

# -------------------------
# Watchdog handler con debounce
# -------------------------

class DebouncedHandler(FileSystemEventHandler):
    def __init__(self, q: Queue, allowed_exts: set[str]):
        super().__init__()
        self.q = q
        self.allowed_exts = allowed_exts

    def _interesting(self, path: Path) -> bool:
        return path.suffix.lower() in self.allowed_exts

    def on_created(self, event):
        if not event.is_directory and self._interesting(Path(event.src_path)):
            self.q.put(("fs", time.time()))

    def on_moved(self, event):
        if not event.is_directory and self._interesting(Path(event.dest_path)):
            self.q.put(("fs", time.time()))

    def on_modified(self, event):
        if not event.is_directory and self._interesting(Path(event.src_path)):
            # spesso le GoPro chiudono il file a “chunk”: il debounce gestirà
            self.q.put(("fs", time.time()))

# -------------------------
# Main watcher loop
# -------------------------

def main():
    setup_logging()

    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini")
    cfg = load_config(cfg_path)

    # cartelle da osservare
    if not cfg.has_option("general", "source_dirs"):
        logging.error("Nessuna root valida: mancano [general].source_dirs")
        sys.exit(2)

    roots = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]
    roots = [Path(r).expanduser() for r in roots if Path(r).expanduser().exists()]
    if not roots:
        logging.error("Nessuna root valida da osservare. Esco..")
        sys.exit(2)

    # estensioni
    allowed = [e.strip().lower() for e in cfg.get("general", "allowed_extensions",
                                                  fallback=".mp4,.mov,.m4v,.avi,.mkv").split(",")]
    allowed_exts = set(allowed)

    # debounce & pianificazioni
    debounce_sec = cfg.getint("watcher", "debounce_seconds", fallback=90)
    rescan_minutes = cfg.getint("watcher", "rescan_minutes", fallback=60)
    pause_check_seconds = cfg.getint("watcher", "pause_check_seconds", fallback=30)

    uploader_path = (SCRIPT_DIR / "tubesync_synology.py").resolve()

    # coda eventi + timer debounce
    q: Queue = Queue()
    timer_lock = threading.Lock()
    debounce_timer: threading.Timer | None = None

    def trigger_run(reason: str):
        active, until = pause_active(cfg)
        if active:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
            logging.info(f"Pausa attiva (quota). Rinvio run: pausa fino a {when}. (trigger: {reason})")
            return
        run_uploader(cfg_path, uploader_path)

    def start_debounce(reason: str):
        nonlocal debounce_timer
        with timer_lock:
            if debounce_timer and debounce_timer.is_alive():
                debounce_timer.cancel()
            debounce_timer = threading.Timer(debounce_sec, trigger_run, args=(reason,))
            debounce_timer.daemon = True
            debounce_timer.start()

    # watchdog setup
    event_handler = DebouncedHandler(q, allowed_exts)
    observer = Observer()
    for r in roots:
        observer.schedule(event_handler, str(r), recursive=True)
        logging.info(f"Osservo: {r}")
    observer.start()

    # 1) run iniziale (se non in pausa)
    trigger_run("initial_scan")

    # 2) thread che consuma la coda e lancia il debounce
    def consumer():
        while True:
            try:
                _evt, _ts = q.get(timeout=1.0)
            except Empty:
                continue
            start_debounce("filesystem")

    threading.Thread(target=consumer, daemon=True).start()

    # 3) thread: controllo scadenza pausa
    def pause_watcher():
        next_fire_if_paused = 0.0
        while True:
            active, until = pause_active(cfg)
            now = time.time()
            if active:
                # pianifica un “wake-up” poco dopo la scadenza
                next_fire_if_paused = max(next_fire_if_paused, until + 1)
            else:
                if next_fire_if_paused and now >= next_fire_if_paused:
                    logging.info("Pausa scaduta: lancio run automatico.")
                    trigger_run("pause_expired")
                    next_fire_if_paused = 0.0
            time.sleep(max(1, pause_check_seconds))

    threading.Thread(target=pause_watcher, daemon=True).start()

    # 4) thread: rescan periodico (opzionale)
    def periodic_rescan():
        if rescan_minutes <= 0:
            return
        interval = max(1, rescan_minutes) * 60
        while True:
            time.sleep(interval)
            logging.info("Rescan periodico: lancio run.")
            trigger_run("periodic_rescan")

    threading.Thread(target=periodic_rescan, daemon=True).start()

    logging.info("TubeSync Watcher attivo. In attesa di eventi..")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logging.info("Watcher terminato.")
        # se c’è un timer in corso, lascialo finire senza rilanciare nulla

if __name__ == "__main__":
    main()
