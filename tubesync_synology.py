#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, json, hashlib, logging, logging.handlers, subprocess, traceback
import sqlite3, smtplib, requests
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

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

class QuotaExceeded(Exception):
    pass

SCRIPT_DIR = Path(__file__).resolve().parent

# ---- path helper: risolve relativi rispetto al config.ini ----
def resolve_relative(cfg_path: Path, raw_value: str) -> Path:
    p = Path(raw_value).expanduser()
    if not p.is_absolute():
        p = cfg_path.parent / p
    return p.resolve()

def get_path_from_cfg(cfg: ConfigParser, cfg_path: Path, section: str, option: str, default_rel: str) -> Path:
    if cfg.has_option(section, option):
        return resolve_relative(cfg_path, cfg.get(section, option))
    return (cfg_path.parent / default_rel).resolve()

# ---- logging (Synology + console opzionale) ----
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
            subprocess.run(["/usr/syno/bin/synologset1", "sys", level, self.event_hex, full], check=False)
        except Exception:
            pass

def setup_logging():
    handlers = [SynologyLogHandler(program="TubeSyncUploader")]
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncUploader[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

def load_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read(cfg_path)
    return cfg

# ---- email ----
def send_email(cfg: ConfigParser, subject: str, body: str):
    if not cfg.getboolean("email", "enabled", fallback=False):
        return
    method = cfg.get("email", "method", fallback="smtp").lower()
    from_email = cfg.get("email", "from_email")
    to_email   = cfg.get("email", "to_email")

    if method == "sendgrid":
        api_key = cfg.get("email", "sendgrid_api_key", fallback=None)
        if not api_key:
            return
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": from_email},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            },
            timeout=30
        )
        if resp.status_code >= 300:
            logging.error(f"Errore SendGrid: {resp.status_code} {resp.text}")
        return

    if method == "smtp_api":
        api_key = cfg.get("email", "smtp2go_api_key", fallback=None)
        api_url = cfg.get("email", "smtp2go_api_url", fallback="https://api.smtp2go.com/v3/email/send")
        if not api_key:
            return
        resp = requests.post(
            api_url,
            headers={"Content-Type": "application/json"},
            json={
                "api_key": api_key,
                "to": [to_email],
                "sender": from_email,
                "subject": subject,
                "text_body": body,
            },
            timeout=30
        )
        if resp.status_code >= 300:
            logging.error(f"Errore SMTP API: {resp.status_code} {resp.text}")
        return

    # fallback SMTP classico (Synology o altro)
    import smtplib
    host = cfg.get("email", "smtp_host")
    port = cfg.getint("email", "smtp_port", fallback=587)
    use_tls = cfg.getboolean("email", "use_tls", fallback=True)
    user = cfg.get("email", "username", fallback=None)
    pwd  = cfg.get("email", "password", fallback=None)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP(host, port, timeout=30) as s:
        if use_tls:
            s.starttls()
        if user and pwd:
            s.login(user, pwd)
        s.sendmail(from_email, [to_email], msg.as_string())

# ---- YouTube auth ----
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

# ---- (il resto del tuo uploader rimane invariato) ----
# Qui includi tutta la logica di scansione, DB, idratazione, upload, quota/pause e invio summary
# ... (la tua versione completa corrente)
