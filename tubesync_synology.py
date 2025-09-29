#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, json, hashlib, logging, logging.handlers, socket, subprocess, traceback
import sqlite3, smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from configparser import ConfigParser

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError, ResumableUploadError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# ---- OAuth scopes ----
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

SCRIPT_DIR = Path(__file__).resolve().parent

# -------------------------
# Utility per path
# -------------------------
def resolve_relative(cfg_path: Path, raw_value: str) -> Path:
    """Risolvi path relativo rispetto al config.ini."""
    p = Path(raw_value).expanduser()
    if not p.is_absolute():
        p = cfg_path.parent / p
    return p.resolve()

def get_path_from_cfg(cfg: ConfigParser, cfg_path: Path, section: str, option: str, default_rel: str) -> Path:
    if cfg.has_option(section, option):
        return resolve_relative(cfg_path, cfg.get(section, option))
    return (cfg_path.parent / default_rel).resolve()

# -------------------------
# Logging
# -------------------------
class SynologyLogHandler(logging.Handler):
    LEVEL_MAP = {
        logging.CRITICAL: "crit",
        logging.ERROR:    "err",
        logging.WARNING:  "warning",
        logging.INFO:     "info",
        logging.DEBUG:    "debug",
    }
    def __init__(self, program="TubeSyncUploader"):
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
    handlers = [SynologyLogHandler(program="TubeSyncUploader")]
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncUploader[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# -------------------------
# Config loader
# -------------------------
def load_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read(cfg_path)
    return cfg

# -------------------------
# YouTube auth
# -------------------------
def get_authenticated_service(cfg: ConfigParser, cfg_path: Path):
    client_secret = get_path_from_cfg(cfg, cfg_path, "general", "client_secret_path", "client_secret.json")
    token_path    = get_path_from_cfg(cfg, cfg_path, "general", "token_path", "token.json")

    if not client_secret.exists():
        raise FileNotFoundError(f"client_secret.json non trovato: {client_secret}")

    creds = None
    if token_path.exists():
        with open(token_path, "r") as f:
            data = json.load(f)
        creds = Credentials.from_authorized_user_info(data, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds, cache_discovery=False)

# -------------------------
# Main (ridotto per esempio)
# -------------------------
def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini")
    cfg = load_config(cfg_path)
    setup_logging()

    yt = get_authenticated_service(cfg, cfg_path)
    logging.info("Autenticazione YouTube OK")

if __name__ == "__main__":
    main()
