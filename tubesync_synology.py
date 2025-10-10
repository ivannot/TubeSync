#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, time, json, hashlib, logging, logging.handlers, sqlite3, smtplib, traceback, os, subprocess
from pathlib import Path
from configparser import ConfigParser
from email.mime.text import MIMEText
from email.utils import formatdate

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

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
    logging.getLogger().name = "TubeSync"

# ========= Config / DB / Utils =========
def load_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(cfg_path)
    return cfg

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

def sha1_of_file(p: Path, block=1024*1024):
    h = hashlib.sha1()
    with p.open("rb") as f:
        while True:
            chunk = f.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def is_allowed_ext(p: Path, allowed_exts):
    return p.suffix.lower() in allowed_exts

def discover_files(source_dirs, allowed_exts, min_size_bytes):
    for s in source_dirs:
        root = Path(s).expanduser()
        if not root.exists():
            logging.warning(f"Cartella sorgente non trovata: {root}")
            syno_log_warn("WARN", f"Cartella sorgente non trovata: {root}")
            continue
        for p in root.rglob("*"):
            if p.is_file() and is_allowed_ext(p, allowed_exts):
                try:
                    size = p.stat().st_size
                    if size < min_size_bytes:
                        continue
                    yield p
                except Exception as e:
                    logging.error(f"Stat fallita per {p}: {e}")
                    syno_log_err("ERROR", f"Stat fallita per {p}: {e}")

def db_get(con, path: Path):
    cur = con.execute("SELECT id, status, size, mtime, sha1, video_id FROM uploads WHERE path = ?", (str(path),))
    return cur.fetchone()

def db_upsert(con, path: Path, size, mtime, sha1, status, video_id=None, error=None):
    now = time.time()
    con.execute("""
        INSERT INTO uploads (path, size, mtime, sha1, status, video_id, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size=excluded.size, mtime=excluded.mtime, sha1=excluded.sha1,
            status=excluded.status, video_id=excluded.video_id, error=excluded.error,
            updated_at=excluded.updated_at
    """, (str(path), size, mtime, sha1, status, video_id, error, now, now))
    con.commit()

# ========= Email =========
def send_email(cfg: ConfigParser, subject: str, body: str):
    if not cfg.getboolean("email", "enabled", fallback=False):
        return
    method = cfg.get("email", "method", fallback=cfg.get("email", "mode", fallback="smtp")).strip().lower()
    from_email = cfg.get("email", "from_email")
    to_email = cfg.get("email", "to_email")

    if method == "smtp2go_api":
        import json as _json, urllib.request as _ur
        api_key = cfg.get("email", "smtp2go_api_key", fallback=None)
        api_url = cfg.get("email", "smtp2go_api_url", fallback="https://api.smtp2go.com/v3/email/send")
        if not api_key:
            logging.error("SMTP2GO_API: api_key mancante")
            syno_log_err("ERROR", "SMTP2GO_API: api_key mancante")
            return
        payload = {"api_key": api_key, "to": [to_email], "sender": from_email, "subject": subject, "text_body": body}
        data = _json.dumps(payload).encode("utf-8")
        req = _ur.Request(api_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=20) as resp:
                logging.info(f"SMTP2GO_API: mail inviata. HTTP {resp.status}")
                syno_log_info("EMAIL", f"SMTP2GO_API: mail inviata. HTTP {resp.status}")
        except Exception as e:
            logging.error(f"SMTP2GO_API: errore invio: {e}")
            syno_log_err("EMAIL_ERR", f"SMTP2GO_API: errore invio: {e}")
        return

    # SMTP standard
    host = cfg.get("email", "smtp_host", fallback="127.0.0.1")
    port = cfg.getint("email", "smtp_port", fallback=587)
    use_tls = cfg.getboolean("email", "use_tls", fallback=True)
    use_ssl = cfg.getboolean("email", "use_ssl", fallback=False)
    user = cfg.get("email", "username", fallback=None)
    pwd = cfg.get("email", "password", fallback=None)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if user and pwd:
                    s.login(user, pwd)
                s.sendmail(from_email, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if use_tls:
                    s.starttls()
                if user and pwd:
                    s.login(user, pwd)
                s.sendmail(from_email, [to_email], msg.as_string())
        logging.info("SMTP: mail inviata.")
        syno_log_info("EMAIL", "SMTP: mail inviata.")
    except Exception as e:
        logging.error(f"SMTP: errore invio: {e}")
        syno_log_err("EMAIL_ERR", f"SMTP: errore invio: {e}")

# ========= YouTube =========
def get_authenticated_service(cfg: ConfigParser):
    client_secret = Path(cfg.get("general", "client_secret_path")).expanduser()
    token_path = Path(cfg.get("general", "token_path")).expanduser()
    if not client_secret.exists():
        msg = f"client_secret.json non trovato: {client_secret}"
        logging.error(msg); syno_log_err("AUTH_ERR", msg)
        raise FileNotFoundError(msg)

    creds = None
    if token_path.exists():
        with open(token_path, "r") as f:
            creds = Credentials.from_authorized_user_info(json.load(f), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            msg = "Token non valido/assente; rigenera token.json e riprova."
            logging.error(msg); syno_log_err("AUTH_ERR", msg)
            raise RuntimeError(msg)

    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def fetch_existing_titles(youtube):
    titles = {}
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        return titles
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    page = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet,status", playlistId=uploads, maxResults=50, pageToken=page
        ).execute()
        for it in resp.get("items", []):
            sn = it.get("snippet", {})
            title = (sn.get("title") or "").strip().lower()
            vid = (sn.get("resourceId", {}) or {}).get("videoId")
            if title and vid:
                titles[title] = vid
        page = resp.get("nextPageToken")
        if not page:
            break
    return titles

def resumable_upload(youtube, file_path: Path, title: str, description: str, privacy: str,
                     category_id: int, chunk_mb: int, max_retries: int):
    body = {
        "snippet": {
            "title": title,
            "description": description or "",
            "categoryId": str(category_id) if category_id else None
        },
        "status": {"privacyStatus": privacy or "private"}
    }
    body["snippet"] = {k: v for k, v in body["snippet"].items() if v is not None}

    media = MediaFileUpload(str(file_path), chunksize=chunk_mb * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry = 0
    backoff = 2

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logging.info(f"[{file_path.name}] Progresso upload: {pct}%")
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504] and retry < max_retries:
                retry += 1
                sleep_s = backoff ** retry
                msg = f"[{file_path.name}] Errore {e.resp.status}, retry {retry}/{max_retries} tra {sleep_s}s"
                logging.warning(msg)
                syno_log_warn("RETRY", msg)
                time.sleep(sleep_s)
                continue
            raise
        except Exception as e:
            if retry < max_retries:
                retry += 1
                sleep_s = backoff ** retry
                msg = f"[{file_path.name}] Eccezione {type(e).__name__}: {e} - retry {retry}/{max_retries} tra {sleep_s}s"
                logging.warning(msg)
                syno_log_warn("RETRY", msg)
                time.sleep(sleep_s)
                continue
            raise

        if response is not None:
            if "id" in response:
                return response["id"]
            raise RuntimeError(f"Risposta inattesa API: {response}")

# ========= Main =========
def main():
    setup_logging()
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("/volume2/TubeSync/config.ini")
    cfg = load_config(cfg_path)

    dbp = Path(cfg.get("general", "db_path", fallback="/volume2/TubeSync/state.db")).expanduser()
    con = ensure_db(dbp)

    source_dirs = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]
    allowed_exts = [e.strip().lower() for e in cfg.get("general", "allowed_extensions").split(",")]
    min_size_mb = cfg.getint("general", "skip_if_smaller_than_mb", fallback=5)
    min_size_bytes = max(0, min_size_mb) * 1024 * 1024

    privacy = cfg.get("general", "privacy", fallback="private")
    category_id = cfg.getint("general", "category_id", fallback=22)
    description = cfg.get("general", "description", fallback="")
    use_sha1 = cfg.getboolean("general", "use_sha1", fallback=False)
    chunk_mb = cfg.getint("general", "chunk_mb", fallback=8)
    max_retries = cfg.getint("general", "max_retries", fallback=8)

    hydrate = cfg.getboolean("general", "hydrate_from_youtube_on_start", fallback=True)
    hydrate_match = cfg.get("general", "hydrate_match", fallback="exact_title").strip().lower()

    subj_prefix = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")
    to_email = cfg.get("email", "to_email", fallback=None)

    # Auth
    syno_log_info("START", "Avvio autenticazione YouTube")
    try:
        yt = get_authenticated_service(cfg)
        syno_log_info("AUTH", "Autenticazione YouTube OK")
    except Exception as e:
        err = f"Autenticazione YouTube fallita: {e}"
        logging.error(err)
        syno_log_err("AUTH_FAIL", err)
        try:
            send_email(cfg, f"{subj_prefix} Auth error", err + "\n\n" + traceback.format_exc())
        except Exception:
            pass
        sys.exit(2)

    existing_title_map = {}
    if hydrate:
        logging.info("Idratazione: scarico elenco titoli dal canale (Uploads)...")
        syno_log_info("HYDRATE", "Idratazione: scarico elenco titoli (Uploads)")
        try:
            existing_title_map = fetch_existing_titles(yt)
            logging.info(f"Idratazione completata: {len(existing_title_map)} video trovati.")
            syno_log_info("HYDRATE", f"Idratazione completata: {len(existing_title_map)} video trovati")
        except Exception as e:
            logging.warning(f"Idratazione fallita (procedo): {e}")
            syno_log_warn("HYDRATE", f"Idratazione fallita (procedo): {e}")

    count_total = count_done = count_skipped = count_errors = count_marked_done = 0

    for p in discover_files(source_dirs, allowed_exts, min_size_bytes):
        count_total += 1
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
            sha1 = sha1_of_file(p) if use_sha1 else None
            title = p.stem
            title_key = title.strip().lower()

            row = db_get(con, p)
            if row:
                _id, status, old_size, old_mtime, old_sha1, video_id = row
                unchanged = (old_size == size and int(old_mtime) == int(mtime) and (not use_sha1 or old_sha1 == sha1))
                if status == "done" and unchanged:
                    count_skipped += 1
                    continue

            if hydrate and hydrate_match == "exact_title":
                found_vid = existing_title_map.get(title_key)
                if found_vid:
                    db_upsert(con, p, size, mtime, sha1, "done", found_vid, None)
                    count_marked_done += 1
                    logging.info(f"[{p.name}] Già presente su YouTube (ID: {found_vid}) → marcato done.")
                    syno_log_info("SKIP", f"{p.name} già su YouTube (ID {found_vid})")
                    continue

            db_upsert(con, p, size, mtime, sha1, "pending", None, None)
            video_id = resumable_upload(yt, p, title, description, privacy, category_id, chunk_mb, max_retries)
            db_upsert(con, p, size, mtime, sha1, "done", video_id, None)
            count_done += 1

            link = f"https://youtu.be/{video_id}"
            logging.info(f"[{p.name}] COMPLETATO: {link}")
            syno_log_info("SUCCESS", f"{p.name} COMPLETATO: {link}")
            send_email(cfg, f"{subj_prefix} OK - {p.name}",
                       f"Caricamento completato.\n\nFile: {p}\nTitolo: {title}\nVideo ID: {video_id}\nLink: {link}\n")

        except Exception as e:
            count_errors += 1
            logging.error(f"[{p.name}] ERRORE: {e}")
            syno_log_err("ERROR", f"{p.name} ERRORE: {e}")
            logging.error(traceback.format_exc())
            try:
                st = p.stat()
                size, mtime = st.st_size, st.st_mtime
            except Exception:
                size = mtime = None
            sha1 = None
            if use_sha1 and p.exists():
                try:
                    sha1 = sha1_of_file(p)
                except Exception:
                    pass
            db_upsert(con, p, size, mtime, sha1, "error", None, str(e))
            send_email(cfg, f"{subj_prefix} ERROR - {p.name}",
                       f"Caricamento FALLITO.\n\nFile: {p}\n\nErrore: {e}\n\nTraceback:\n{traceback.format_exc()}")

    summary = (f"Totale trovati: {count_total}, Caricati ora: {count_done}, "
               f"Segnati già presenti: {count_marked_done}, Skippati invariati: {count_skipped}, Errori: {count_errors}")
    logging.info(summary)
    syno_log_info("SUMMARY", summary)

    if to_email and cfg.getboolean("email", "send_summary", fallback=True):
        if cfg.getboolean("email", "send_summary_when_noop", fallback=False) or any([count_done, count_marked_done, count_errors]):
            send_email(cfg, f"{subj_prefix} Summary", summary)

if __name__ == "__main__":
    main()
