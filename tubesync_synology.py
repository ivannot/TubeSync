#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, json, hashlib, logging, logging.handlers, socket, subprocess, traceback
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

# ---- OAuth scopes ----
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

class QuotaExceeded(Exception):
    pass

# -------------------------
# Logging (Synology + syslog)
# -------------------------

class SynologyLogHandler(logging.Handler):
    LEVEL_MAP = {
        logging.CRITICAL: "err",
        logging.ERROR:    "err",
        logging.WARNING:  "warn",
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

SCRIPT_DIR = Path(__file__).resolve().parent

def setup_logging():
    handlers = [SynologyLogHandler(program="TubeSyncUploader")]
    try:
        if os.path.exists("/dev/log"):
            handlers.append(logging.handlers.SysLogHandler(address="/dev/log"))
    except Exception:
        pass
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("TubeSyncUploader[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# -------------------------
# Config & utils
# -------------------------

def load_config(cfg_path: Path) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=('#', ';'))
    cfg.read(cfg_path)
    return cfg

def get_path_from_cfg(cfg, section, option, default_rel):
    if cfg.has_option(section, option):
        return Path(cfg.get(section, option)).expanduser().resolve()
    return (SCRIPT_DIR / default_rel).resolve()

def ensure_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("""
    CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE,
        size INTEGER,
        mtime REAL,
        sha1 TEXT,
        status TEXT,
        video_id TEXT,
        error TEXT,
        created_at REAL,
        updated_at REAL
    )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status)")
    con.commit()
    return con

def sha1_of_file(p: Path):
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_allowed_ext(p: Path, allowed_exts):
    return p.suffix.lower() in allowed_exts

def discover_files(source_dirs, allowed_exts, min_size_bytes):
    for s in source_dirs:
        root = Path(s).expanduser()
        if not root.exists():
            logging.warning(f"Cartella sorgente non trovata: {root}")
            continue
        for p in root.rglob("*"):
            if p.is_file() and is_allowed_ext(p, allowed_exts):
                if p.stat().st_size >= min_size_bytes:
                    yield p

def db_get(con, path: Path):
    return con.execute("SELECT id, status, size, mtime, sha1, video_id FROM uploads WHERE path = ?", (str(path),)).fetchone()

def db_upsert(con, path: Path, size, mtime, sha1, status, video_id=None, error=None):
    now = time.time()
    con.execute("""
    INSERT INTO uploads (path, size, mtime, sha1, status, video_id, error, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        size=excluded.size,
        mtime=excluded.mtime,
        sha1=excluded.sha1,
        status=excluded.status,
        video_id=excluded.video_id,
        error=excluded.error,
        updated_at=excluded.updated_at
    """, (str(path), size, mtime, sha1, status, video_id, error, now, now))
    con.commit()

# -------------------------
# Email
# -------------------------

def send_email(cfg, subject, body):
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

    if method == "smtp":
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

# -------------------------
# YouTube
# -------------------------

def get_authenticated_service(cfg):
    client_secret = get_path_from_cfg(cfg, "general", "client_secret_path", "client_secret.json")
    token_path    = get_path_from_cfg(cfg, "general", "token_path", "token.json")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def resumable_upload(youtube, file_path, title, description, privacy, category_id, chunk_mb, max_retries, made_for_kids):
    body = {
        "snippet": {"title": title, "description": description, "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids},
    }
    media = MediaFileUpload(str(file_path), chunksize=chunk_mb*1024*1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                logging.info(f"[{file_path.name}] Progresso upload: {int(status.progress()*100)}%")
            if response:
                return response.get("id")
        except (HttpError, ResumableUploadError) as e:
            raise
    return None

# -------------------------
# Main
# -------------------------

def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR / "config.ini"
    cfg = load_config(cfg_path)
    setup_logging()

    db_path = get_path_from_cfg(cfg, "general", "db_path", "state.db")
    con = ensure_db(db_path)

    source_dirs = [s.strip() for s in cfg.get("general", "source_dirs").split(",")]
    allowed_exts = [e.strip().lower() for e in cfg.get("general", "allowed_extensions").split(",")]
    min_size_bytes = cfg.getint("general", "skip_if_smaller_than_mb", fallback=5) * 1024 * 1024
    privacy = cfg.get("general", "privacy", fallback="private")
    category_id = cfg.getint("general", "category_id", fallback=22)
    description = cfg.get("general", "description", fallback="")
    made_for_kids = cfg.getboolean("general", "made_for_kids", fallback=False)
    chunk_mb = cfg.getint("general", "chunk_mb", fallback=8)
    max_retries = cfg.getint("general", "max_retries", fallback=8)

    to_email = cfg.get("email", "to_email", fallback=None)
    subj_prefix = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")

    yt = get_authenticated_service(cfg)

    count_total = count_done = count_skipped = count_errors = 0

    for p in discover_files(source_dirs, allowed_exts, min_size_bytes):
        count_total += 1
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
            row = db_get(con, p)
            if row and row[1] == "done":
                count_skipped += 1
                continue
            db_upsert(con, p, size, mtime, None, "pending")
            vid = resumable_upload(yt, p, p.stem, description, privacy, category_id, chunk_mb, max_retries, made_for_kids)
            db_upsert(con, p, size, mtime, None, "done", vid)
            count_done += 1
            send_email(cfg, f"{subj_prefix} OK — {p.name}", f"Caricato {p}\nID: {vid}\n")
        except Exception as e:
            count_errors += 1
            db_upsert(con, p, p.stat().st_size, p.stat().st_mtime, None, "error", None, str(e))
            send_email(cfg, f"{subj_prefix} ERROR — {p.name}", f"Errore su {p}:\n{e}\n{traceback.format_exc()}")

    summary = f"Totale trovati: {count_total}, Caricati ora: {count_done}, Skippati: {count_skipped}, Errori: {count_errors}"
    logging.info(summary)

    send_summary = cfg.getboolean("email", "send_summary", fallback=True)
    send_noop    = cfg.getboolean("email", "send_summary_when_noop", fallback=False)
    is_noop = (count_done == 0 and count_errors == 0)

    if send_summary and to_email:
        if (not is_noop) or (is_noop and send_noop):
            send_email(cfg, f"{subj_prefix} Summary", summary)

if __name__ == "__main__":
    main()
