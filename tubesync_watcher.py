#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, subprocess, logging, time
from pathlib import Path
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

SCRIPT_DIR = Path(__file__).resolve().parent

def resolve_relative(cfg_path: Path, raw_value: str) -> Path:
    p = Path(raw_value).expanduser()
    if not p.is_absolute():
        p = cfg_path.parent / p
    return p.resolve()

def load_config(cfg_path: Path) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read(cfg_path)
    return cfg

class WatcherHandler(FileSystemEventHandler):
    def __init__(self, cfg_path, debounce_seconds, uploader):
        super().__init__()
        self.cfg_path = cfg_path
        self.debounce_seconds = debounce_seconds
        self.last_event = 0
        self.uploader = uploader

    def on_any_event(self, event):
        now = time.time()
        if now - self.last_event < self.debounce_seconds:
            return
        self.last_event = now
        logging.info("Evento FS → lancio uploader")
        subprocess.Popen([sys.executable, str(self.uploader), str(self.cfg_path)])

def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini")
    cfg = load_config(cfg_path)

    debounce = cfg.getint("watcher", "debounce_seconds", fallback=90)
    uploader = resolve_relative(cfg_path, cfg.get("paths", "uploader_path", fallback="tubesync_synology.py"))

    event_handler = WatcherHandler(cfg_path, debounce, uploader)
    observer = Observer()
    for d in cfg.get("general", "source_dirs").split(","):
        dpath = resolve_relative(cfg_path, d.strip())
        observer.schedule(event_handler, str(dpath), recursive=True)
        logging.info(f"Osservo: {dpath}")

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
