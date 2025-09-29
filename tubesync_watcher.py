#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, logging, logging.handlers, subprocess, threading
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

SCRIPT_DIR = Path(__file__).resolve().parent

# -------------------------
# Logging (Synology + console opzionale)
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
        # IMPORTANTISSIMO: serve il prefisso 0x
        self.event_hex = os.getenv("TS_EVENT_HEX", "0x11100000")

    def emit(self, record):
        try:
            level = self.LEVEL_MAP.get(record.levelno, "info")
            msg   = self.format(record)
            full  = f"{self.program}: {msg}"
            subprocess.run(
                ["/usr/syno/bin/synologset1", "sys", level, self.event_hex, full],
                check=False
            )
        except Exception:
            pass

def setup_logging():
    handlers = [SynologyLogHandler(program="TubeSyncWatcher")]
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncWatcher[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# -------------------------
# Config & path helpers
# -------------------------
def load_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=('#',';'))
    cfg.read(cfg_path)
    return cfg

def resolve_relative(base: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()

def get_pause_file(cfg: ConfigParser, cfg_path: Path) -> Path:
    raw = cfg.get("general", "pause_file", fallback=".pause_until")
    return resolve_relative(cfg_path.parent, raw)

def pause_active(pause_file: Path):
    """Ritorna timestamp 'until' se pausa attiva, altrimenti None. Se scaduta, rimuove il file."""
    if not pause_file.exists():
        return None
    try:
        until = float(pause_file.read_text().strip())
    except Exception:
        try: pause_file.unlink()
        except Exception: pass
        return None
    now = time.time()
    if now < until:
        return until
    # scaduta → rimuovi
    try: pause_file.unlink()
    except Exception: pass
    return None

# -------------------------
# Watchdog handler (solo created/moved) + debounce
# -------------------------
class DebouncedHandler(FileSystemEventHandler):
    def __init__(self, cfg_path: Path, cfg: ConfigParser):
        super().__init__()
        self.cfg_path = cfg_path
        self.cfg = cfg

        self.debounce_seconds = cfg.getint("watcher", "debounce_seconds", fallback=300)
        self.pause_check_sec  = cfg.getint("watcher", "pause_check_seconds", fallback=30)

        # uploader state
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = False
        self._ran_once = False

        self.pause_file = get_pause_file(cfg, cfg_path)

    # ——— Filtra SOLO created/moved ———
    def on_created(self, event: FileCreatedEvent):
        if event.is_directory: 
            return
        self._debounce("created")

    def on_moved(self, event: FileMovedEvent):
        # event.dest_path è il nuovo percorso
        self._debounce("moved")

    # Niente on_modified: è la fonte principale di “rumore” su Synology

    def _debounce(self, reason: str):
        with self._lock:
            if self._running:
                # uploader già in corso → non riprogrammare
                return
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._fire, kwargs={"reason": "filesystem"})
            self._timer.daemon = True
            self._timer.start()
            logging.debug(f"Evento FS → debounce {self.debounce_seconds}s (reason={reason}).")

    def _fire(self, reason: str):
        with self._lock:
            self._timer = None
            # pausa attiva?
            until = pause_active(self.pause_file)
            if until:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
                logging.info(f"In pausa (quota) fino a {when} — trigger '{reason}' rimandato.")
                # riprova dopo pause_check_sec
                self._timer = threading.Timer(self.pause_check_sec, self._fire, kwargs={"reason": reason})
                self._timer.daemon = True
                self._timer.start()
                return

            # lancia uploader
            self._running = True

        # fuori dal lock eseguo il comando
        trigger_name = "initial_scan" if not self._ran_once and reason == "initial" else reason
        logging.info(f"Trigger '{trigger_name}': lancio uploader.")
        rc = run_uploader(self.cfg_path)
        if rc != 0:
            logging.error(f"Uploader ha restituito codice {rc}.")

        with self._lock:
            self._running = False
            self._ran_once = True
            logging.info("Uploader terminato, watcher pronto.")

# -------------------------
# Uploader runner
# -------------------------
def run_uploader(cfg_path: Path) -> int:
    python = sys.executable                 # usa lo stesso interprete che esegue il watcher
    uploader = (SCRIPT_DIR / "tubesync_synology.py").resolve()
    cmd = [str(python), str(uploader), str(cfg_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            logging.debug(f"[uploader stdout] {proc.stdout[-4000:].strip()}")
        if proc.stderr:
            logging.debug(f"[uploader stderr] {proc.stderr[-4000:].strip()}")
        return proc.returncode
    except Exception as e:
        logging.error(f"Errore nell’esecuzione dell’uploader: {e}")
        return 1

# -------------------------
# Main loop (observer + rescan periodico)
# -------------------------
def main():
    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} config.ini", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(sys.argv[1]).expanduser().resolve()
    cfg = load_config(cfg_path)
    setup_logging()

    # Roots da osservare (risolte rispetto al config.ini)
    if not cfg.has_option("general", "source_dirs"):
        logging.error("Nessuna sorgente definita in [general] source_dirs")
        sys.exit(2)
    roots = []
    for raw in cfg.get("general", "source_dirs").split(","):
        raw = raw.strip()
        if not raw:
            continue
        p = resolve_relative(cfg_path.parent, raw)
        if p.exists():
            roots.append(p)
        else:
            logging.warning(f"Sorgente inesistente: {p}")
    if not roots:
        logging.error("Nessuna root valida da osservare. Esco..")
        sys.exit(2)

    handler = DebouncedHandler(cfg_path, cfg)

    obs = Observer()
    for r in roots:
        obs.schedule(handler, str(r), recursive=True)
        logging.info(f"Osservo: {r}")
    obs.start()

    # Trigger iniziale (solo una volta, fuori da eventi FS)
    # - Se c'è pausa attiva, partirà appena scade (loggando che è rimandato)
    threading.Timer(0.1, handler._fire, kwargs={"reason": "initial"}).start()

    # Rescan periodico
    rescan_minutes = cfg.getint("watcher", "rescan_minutes", fallback=60)
    pause_file = get_pause_file(cfg, cfg_path)
    last_rescan = time.time()

    try:
        while True:
            time.sleep(1)
            if rescan_minutes > 0:
                now = time.time()
                if now - last_rescan >= rescan_minutes * 60:
                    last_rescan = now
                    # Rispetta pausa e stato running
                    if pause_active(pause_file):
                        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pause_active(pause_file)))
                        logging.info(f"Rescan periodico saltato: in pausa fino a {when}.")
                        continue
                    # Evita overlap
                    if handler._running:
                        logging.info("Rescan periodico saltato: uploader già in esecuzione.")
                        continue
                    logging.info("Trigger 'periodic_rescan': lancio uploader.")
                    # lancia sincrono
                    rc = run_uploader(cfg_path)
                    if rc != 0:
                        logging.error(f"Uploader (periodic_rescan) ha restituito codice {rc}.")
                    else:
                        logging.info("Uploader (periodic_rescan) completato.")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            obs.stop()
            obs.join(timeout=5)
        except Exception:
            pass

if __name__ == "__main__":
    main()
