#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, json, hashlib, logging, logging.handlers, subprocess, traceback
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

# ---- OAuth scopes: upload + read-only (idratazione/verifica) ----
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

class QuotaExceeded(Exception):
    pass

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
    # Fallback syslog locale
    try:
        sock = "/dev/log" if os.path.exists("/dev/log") else ("/run/systemd/journal/dev-log" if os.path.exists("/run/systemd/journal/dev-log") else None)
        if sock:
            handlers.append(logging.handlers.SysLogHandler(address=sock, facility=logging.handlers.SysLogHandler.LOG_LOCAL0))
    except Exception:
        pass
    # Opzioni debug opzionali
    if os.getenv("TS_FILE"):
        try: handlers.append(logging.FileHandler(os.getenv("TS_FILE")))
        except Exception: pass
    if os.getenv("TS_CONSOLE") == "1":
        handlers.append(logging.StreamHandler())

    fmt = logging.Formatter("TubeSyncUploader[%(process)d]: [%(levelname)s] %(message)s")
    for h in handlers: h.setFormatter(fmt)

    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

# -------------------------
# Config & util
# -------------------------
def load_config(cfg_path: Path) -> ConfigParser:
    if not cfg_path.exists():
        print(f"Config non trovato: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read(cfg_path)
    return cfg

def get_path_from_cfg(cfg: ConfigParser, section: str, option: str, default_rel: str) -> Path:
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
        status TEXT,          -- pending|done|error
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
            if not chunk: break
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
                try:
                    size = p.stat().st_size
                    if size < min_size_bytes:
                        continue
                    yield p
                except Exception as e:
                    logging.error(f"Stat fallita per {p}: {e}")

def db_get(con, path: Path):
    cur = con.execute("SELECT id, status, size, mtime, sha1, video_id FROM uploads WHERE path = ?", (str(path),))
    return cur.fetchone()

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

def send_email(cfg: ConfigParser, subject: str, body: str):
    if not cfg.getboolean("email", "enabled", fallback=False):
        return
    method = cfg.get("email", "method", fallback="smtp").strip().lower()
    from_email = cfg.get("email", "from_email")
    to_email = cfg.get("email", "to_email")

    # Messaggio
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    if method == "sendgrid":
        import requests
        api_key = cfg.get("email", "sendgrid_api_key")
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            r = requests.post("https://api.sendgrid.com/v3/mail/send", headers=headers, json=payload, timeout=15)
            if r.status_code >= 300:
                logging.error(f"SendGrid error {r.status_code}: {r.text}")
        except Exception as e:
            logging.error(f"SendGrid exception: {e}")
        return

    if method == "smtp2go":
        import requests
        api_key = cfg.get("email", "smtp2go_api_key")
        payload = {
            "api_key": api_key,
            "to": [to_email],
            "sender": from_email,
            "subject": subject,
            "text_body": body,
        }
        try:
            r = requests.post("https://api.smtp2go.com/v3/email/send", json=payload, timeout=15)
            if r.status_code != 200:
                logging.error(f"SMTP2GO error {r.status_code}: {r.text}")
        except Exception as e:
            logging.error(f"SMTP2GO exception: {e}")
        return

    # SMTP classico
    host = cfg.get("email", "smtp_host")
    port = cfg.getint("email", "smtp_port", fallback=587)
    use_tls = cfg.getboolean("email", "use_tls", fallback=True)
    use_ssl = cfg.getboolean("email", "use_ssl", fallback=False)
    user = cfg.get("email", "username", fallback=None)
    pwd = cfg.get("email", "password", fallback=None)

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if user and pwd: s.login(user, pwd)
            s.sendmail(from_email, [to_email], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if use_tls: s.starttls()
            if user and pwd: s.login(user, pwd)
            s.sendmail(from_email, [to_email], msg.as_string())

# -------------------------
# YouTube helpers
# -------------------------
def get_authenticated_service(cfg: ConfigParser):
    client_secret = get_path_from_cfg(cfg, "general", "client_secret_path", "client_secret.json")
    token_path    = get_path_from_cfg(cfg, "general", "token_path", "token.json")

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

def fetch_existing_titles(youtube):
    """Ritorna dict { titolo_lower: video_id } per tutta la playlist 'Uploads'."""
    titles = {}
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        return titles
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet,status",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token
        ).execute()
        for it in resp.get("items", []):
            sn = it.get("snippet", {})
            title = (sn.get("title") or "").strip().lower()
            vid = (sn.get("resourceId", {}) or {}).get("videoId")
            if title and vid:
                titles[title] = vid
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return titles

def youtube_videos_exist(youtube, ids):
    """Ritorna set di video_id che ESISTONO (videos.list part=snippet/status), input <=50 per batch."""
    existing = set()
    for i in range(0, len(ids), 50):
        chunk = ids[i:i+50]
        resp = youtube.videos().list(part="status", id=",".join(chunk)).execute()
        for it in resp.get("items", []):
            vid = it.get("id")
            if vid:
                existing.add(vid)
    return existing

def _parse_error_reasons(exc) -> set:
    reasons = set()
    content = None
    try:
        content = getattr(exc, "content", None)
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")
        if isinstance(content, str) and content:
            obj = json.loads(content)
            err = obj.get("error") or {}
            errs = err.get("errors") or []
            for e in errs:
                r = (e.get("reason") or "").lower()
                if r:
                    reasons.add(r)
            if not reasons and "details" in err:
                for d in err["details"]:
                    r = (d.get("reason") or "").lower()
                    if r:
                        reasons.add(r)
    except Exception:
        pass
    try:
        msg = str(exc)
        for key in ("quotaexceeded", "userratelimitexceeded", "dailylimitexceeded", "uploadlimitexceeded"):
            if key in msg.lower():
                reasons.add(key)
    except Exception:
        pass
    return reasons

def _is_quota_reason(reasons: set) -> bool:
    for key in ("quotaexceeded", "userratelimitexceeded", "dailylimitexceeded", "uploadlimitexceeded"):
        if key in reasons:
            return True
    return False

def resumable_upload(youtube, file_path: Path, title: str, description: str, privacy: str, category_id: int, chunk_mb: int, max_retries: int, made_for_kids: bool):
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids)
        }
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
            if response is not None:
                if "id" in response:
                    return response["id"]
                else:
                    raise RuntimeError(f"Risposta inattesa API: {response}")

        except (HttpError, ResumableUploadError) as e:
            reasons = _parse_error_reasons(e)

            # Stop immediato in caso di quota/rate limit/upload limit
            sc = getattr(e, "resp", None).status if getattr(e, "resp", None) else None
            if (sc in (400, 403, 429)) and _is_quota_reason(reasons):
                raise QuotaExceeded(",".join(sorted(reasons)) or "quotaExceeded") from e

            # Retri per errori temporanei
            if sc in (500, 502, 503, 504) and retry < max_retries:
                retry += 1
                sleep_s = backoff ** retry
                logging.warning(f"[{file_path.name}] Errore temporaneo {sc}, retry {retry}/{max_retries} tra {sleep_s}s")
                time.sleep(sleep_s)
                continue
            raise

        except Exception as e:
            if retry < max_retries:
                retry += 1
                sleep_s = backoff ** retry
                logging.warning(f"[{file_path.name}] Eccezione {type(e).__name__}: {e} — retry {retry}/{max_retries} tra {sleep_s}s")
                time.sleep(sleep_s)
                continue
            raise

# -------------------------
# Pausa quota
# -------------------------
def get_pause_path(cfg: ConfigParser):
    return get_path_from_cfg(cfg, "general", "pause_file", ".pause_until")

def check_pause(cfg: ConfigParser):
    pf = get_pause_path(cfg)
    if pf.exists():
        try:
            until = float(pf.read_text().strip())
            if time.time() < until:
                return until
            pf.unlink(missing_ok=True)
        except Exception:
            pf.unlink(missing_ok=True)
    return None

def set_pause(cfg: ConfigParser, minutes: int):
    pf = get_pause_path(cfg)
    pf.parent.mkdir(parents=True, exist_ok=True)
    until = time.time() + max(1, int(minutes)) * 60
    pf.write_text(str(until))
    return until

# -------------------------
# Re-validate: rimetti pending se il video_id non esiste più
# -------------------------
def revalidate_done_entries(cfg: ConfigParser, con: sqlite3.Connection, youtube):
    reupload_flag = cfg.getboolean("general", "reupload_if_missing_on_youtube", fallback=False)
    if not reupload_flag:
        return 0

    cur = con.execute("SELECT path, video_id FROM uploads WHERE status='done' AND video_id IS NOT NULL")
    rows = cur.fetchall()
    if not rows:
        return 0

    ids = [vid for (_p, vid) in rows if vid]
    # Batch 50
    missing_total = 0
    for i in range(0, len(ids), 50):
        chunk = ids[i:i+50]
        existing = youtube_videos_exist(youtube, chunk)
        missing = set(chunk) - existing
        if not missing:
            continue
        missing_total += len(missing)
        # Mark pending those missing
        for (p, vid) in rows:
            if vid in missing:
                try:
                    st = Path(p).stat()
                    size, mtime = st.st_size, st.st_mtime
                except Exception:
                    size = mtime = None
                db_upsert(con, Path(p), size, mtime, None, "pending", None, "Missing on YouTube; scheduled for re-upload")
                logging.info(f"[REUPLOAD] VideoID assente su YouTube → pending: {p} (was {vid})")

    if missing_total:
        logging.info(f"Re-validate: {missing_total} elementi segnati pending (rimossi da YouTube).")
    else:
        logging.info("Re-validate: nessun video mancante su YouTube.")
    return missing_total

# -------------------------
# Main
# -------------------------
def main():
    cfg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else (SCRIPT_DIR / "config.ini")
    cfg = load_config(cfg_path)
    setup_logging()

    # pausa?
    paused_until = check_pause(cfg)
    if paused_until:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(paused_until))
        logging.warning(f"Pausa attiva fino a {when} per quota limit. Esco.")
        return

    # DB
    db_path = get_path_from_cfg(cfg, "general", "db_path", "state.db")
    con = ensure_db(db_path)

    # auth
    try:
        yt = get_authenticated_service(cfg)
    except Exception as e:
        err = f"Autenticazione YouTube fallita: {e}"
        logging.error(err)
        subj_prefix = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")
        send_email(cfg, f"{subj_prefix} Auth error", err + "\n\n" + traceback.format_exc())
        sys.exit(2)

    # Re-validate: se un video 'done' non esiste più su YouTube → pending
    try:
        revalidate_done_entries(cfg, con, yt)
    except Exception as e:
        logging.warning(f"Re-validate fallita: {e}")

    # Idratazione (mappa titoli esistenti)
    existing_title_map = {}
    if cfg.getboolean("general", "hydrate_from_youtube_on_start", fallback=True):
        try:
            logging.info("Idratazione: scarico elenco titoli dal canale (playlist Uploads)...")
            existing_title_map = fetch_existing_titles(yt)
            logging.info(f"Idratazione completata: trovati {len(existing_title_map)} video esistenti.")
        except Exception as e:
            logging.warning(f"Idratazione fallita (procedo comunque): {e}")

    # sorgenti & filtri
    if not cfg.has_option("general", "source_dirs"):
        logging.error("Nessuna sorgente in [general] source_dirs")
        sys.exit(2)
    source_dirs = [s.strip() for s in cfg.get("general", "source_dirs").split(",") if s.strip()]
    allowed_exts = [e.strip().lower() for e in cfg.get("general", "allowed_extensions", fallback=".mp4,.mov,.m4v,.avi,.mkv").split(",")]
    min_size_mb = cfg.getint("general", "skip_if_smaller_than_mb", fallback=5)
    min_size_bytes = max(0, min_size_mb) * 1024 * 1024

    # upload options
    privacy       = cfg.get("general", "privacy", fallback="private")
    category_id   = cfg.getint("general", "category_id", fallback=22)
    description   = cfg.get("general", "description", fallback="")
    made_for_kids = cfg.getboolean("general", "made_for_kids", fallback=False)
    use_sha1      = cfg.getboolean("general", "use_sha1", fallback=False)
    chunk_mb      = cfg.getint("general", "chunk_mb", fallback=8)
    max_retries   = cfg.getint("general", "max_retries", fallback=8)

    # mail
    subj_prefix = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")
    to_email    = cfg.get("email", "to_email", fallback=None)

    count_total = count_done = count_skipped = count_errors = count_marked_done = 0

    for p in discover_files(source_dirs, allowed_exts, min_size_bytes):
        count_total += 1
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
            sha1 = sha1_of_file(p) if use_sha1 else None
            title = p.stem
            title_key = title.strip().lower()

            # Stato DB
            row = db_get(con, p)
            if row:
                _id, status, old_size, old_mtime, old_sha1, video_id = row
                unchanged = (old_size == size and int(old_mtime) == int(mtime) and (not use_sha1 or old_sha1 == sha1))
                if status == "done" and unchanged:
                    # (Re-validate è stata fatta già all’avvio; se il video non esisteva
                    # sarebbe stato messo pending. Qui possiamo skippare tranquilli.)
                    count_skipped += 1
                    continue

            # già su YT (per titolo)?
            if cfg.getboolean("general", "hydrate_from_youtube_on_start", fallback=True):
                if title_key in existing_title_map:
                    vid_found = existing_title_map[title_key]
                    db_upsert(con, p, size, mtime, sha1, "done", vid_found, None)
                    count_marked_done += 1
                    logging.info(f"[{p.name}] Già presente su YouTube (ID: {vid_found}) → marcato done.")
                    continue

            # upload
            db_upsert(con, p, size, mtime, sha1, "pending", None, None)
            video_id = resumable_upload(yt, p, title, description, privacy, category_id, chunk_mb, max_retries, made_for_kids)
            db_upsert(con, p, size, mtime, sha1, "done", video_id, None)
            count_done += 1

            link = f"https://youtu.be/{video_id}"
            subject = f"{subj_prefix} OK — {p.name}"
            body = f"Caricamento completato.\n\nFile: {p}\nTitolo: {title}\nVideo ID: {video_id}\nLink: {link}\n"
            send_email(cfg, subject, body)
            logging.info(f"[{p.name}] COMPLETATO: {link}")

        except QuotaExceeded as e:
            count_errors += 1
            reason = str(e) or "quotaExceeded"
            cooldown_min = cfg.getint("general", "quota_cooldown_minutes", fallback=120)
            until = set_pause(cfg, cooldown_min)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
            logging.error(f"Limite raggiunto ({reason}): pausa di {cooldown_min}m (fino a {when}). Stop immediato.")
            subject = f"{subj_prefix} Quota exceeded — pausa fino a {when}"
            body = (f"Limite raggiunto ({reason}).\n"
                    f"Lo script va in PAUSA per {cooldown_min} minuti (fino a {when}).\n"
                    f"Nessun altro file verrà processato in questo run.")
            send_email(cfg, subject, body)
            return  # stop, niente spam

        except Exception as e:
            count_errors += 1
            logging.error(f"[{p.name}] ERRORE: {e}")
            logging.error(traceback.format_exc())
            try:
                st = p.stat()
                size, mtime = st.st_size, st.st_mtime
            except Exception:
                size = mtime = None
            sha1 = None
            if use_sha1 and p.exists():
                try: sha1 = sha1_of_file(p)
                except Exception: pass
            db_upsert(con, p, size, mtime, sha1, "error", None, str(e))

            subject = f"{subj_prefix} ERROR — {p.name}"
            body = f"Caricamento FALLITO.\n\nFile: {p}\n\nErrore: {e}\n\nTraceback:\n{traceback.format_exc()}"
            send_email(cfg, subject, body)

    summary = (f"Totale trovati: {count_total}, "
               f"Caricati ora: {count_done}, "
               f"Segnati già presenti: {count_marked_done}, "
               f"Skippati invariati: {count_skipped}, "
               f"Errori: {count_errors}")
    logging.info(summary)

    # Evita mail “vuota” se non è successo nulla
    if to_email and (count_done > 0 or count_errors > 0):
        send_email(cfg, f"{subj_prefix} Summary", summary)

if __name__ == "__main__":
    main()
