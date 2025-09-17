#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TubeSync Watcher
- Monitora SOLO le cartelle in `source_dirs` (ricorsivo)
- Filtra eventi SOLO per le estensioni in `allowed_extensions`
- Debounce configurabile (watcher.debounce_seconds, default 90s)
- Lancia l'uploader con lo STESSO interprete Python (sys.executable)
- Default percorsi relativi alla cartella di questo script (sovrascrivibili via config.ini)
- Log su Log Center (synologset1), fallback syslog / file (TS_FILE) / console (TS_CONSOLE)
"""

import sys, time, threading, subprocess, logging, logging.handlers, os, socket
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

# ------- Logging (Synology first) -------
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

for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

handlers = [SynologyLogHandler(program="TubeSyncWatcher")]

try:
    sock = "/dev/log" if os.path.exists("/dev/log") else ("/run/log" if os.path.exists("/run/log") else None)
    if sock:
        handlers.append(logging.handlers.SysLogHandler(
            address=sock,
            facility=logging.handlers.SysLogHandler.LOG_LOCAL0
        ))
except Exception:
    pass

if os.getenv("TS_FILE"):
    try:
        handlers.append(logging.FileHandler(os.getenv("TS_FILE")))
    except Exception:
        pass
if os.getenv("TS_CONSOLE") == "1":
    handlers.append(logging.StreamHandler())

fmt = logging.Formatter("TubeSyncWatcher[%(process)d]: [%(levelname)s] %(message)s")
for h in handlers: h.setFormatter(fmt)
logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger("TubeSyncWatcher")

# ------- Config / util -------
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DEBOUNCE_SECONDS = 90

def load_config(cfg_path: Path) -> ConfigParser:
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

class DebouncedRunner:
    def __init__(self, cmd, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS):
        self.cmd = cmd
        self.debounce = debounce_seconds
        self._timer = None
        self._lock = threading.Lock()

    def trigger(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self):
        logger.info(f"Esecuzione comando: {' '.join(self.cmd)}")
        try:
            proc = subprocess.Popen(self.cmd)
            code = proc.wait()
            logger.info(f"Comando terminato con exit code {code}")
        except Exception as e:
            logger.exception(f"Errore nell'esecuzione del comando: {e}")

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

def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini")
    cfg = load_config(cfg_path)

    # sorgenti
    if not cfg.has_option("general", "source_dirs"):
        logger.error("source_dirs mancante in [general] del config.ini")
        sys.exit(2)
    roots = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]

    # estensioni
    exts = [e.strip() for e in cfg.get("general", "allowed_extensions", fallback=".mp4,.mov,.m4v,.avi,.mkv").split(",") if e.strip()]
    patterns = build_patterns_from_extensions(exts)
    logger.info(f"Estensioni monitorate: {patterns}")

    # debounce
    debounce_seconds = cfg.getint("watcher", "debounce_seconds", fallback=DEFAULT_DEBOUNCE_SECONDS)

    # path uploader (default: stesso folder dello script; sovrascrivibile in [paths])
    uploader_path = Path(cfg.get("paths", "uploader_path", fallback=str(SCRIPT_DIR / "tubesync_synology.py"))).resolve()

    # comando: usa lo stesso interprete del watcher
    cmd = [sys.executable, str(uploader_path), str(cfg_path)]
    runner = DebouncedRunner(cmd, debounce_seconds=debounce_seconds)

    # prima scansione subito
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
