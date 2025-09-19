#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from configparser import ConfigParser

# opzionali
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
except ImportError:
    SendGridAPIClient = None

import requests


def load_cfg(path: str) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    read = cfg.read(path)
    if not read:
        print(f"❌ Config non trovato o non leggibile: {path}", file=sys.stderr)
        sys.exit(2)
    return cfg


def test_sendgrid(cfg: ConfigParser) -> None:
    if not SendGridAPIClient:
        print("❌ Libreria SendGrid non disponibile. Esegui: pip install sendgrid", file=sys.stderr)
        sys.exit(3)

    api_key    = cfg.get("email", "sendgrid_api_key", fallback=None)
    from_email = cfg.get("email", "from_email")
    to_email   = cfg.get("email", "to_email")
    prefix     = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")

    subject = prefix + " Test email"
    body    = "This is a TubeSync email test via SendGrid."

    try:
        message = Mail(from_email=from_email, to_emails=to_email, subject=subject, plain_text_content=body)
        sg = SendGridAPIClient(api_key)
        resp = sg.send(message)
        print(f"✅ SendGrid OK — HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ SendGrid ERROR — {e}", file=sys.stderr)
        sys.exit(5)


def test_smtp2go_api(cfg: ConfigParser) -> None:
    api_key   = cfg.get("email", "smtp2go_api_key", fallback=None)
    api_url   = cfg.get("email", "smtp2go_api_url", fallback="https://api.smtp2go.com/v3/email/send")
    from_email= cfg.get("email", "from_email")
    to_email  = cfg.get("email", "to_email")
    prefix    = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")

    if not api_key:
        print("❌ Manca 'smtp2go_api_key' in [email]", file=sys.stderr)
        sys.exit(4)

    subject = prefix + " Test email"
    body    = "This is a TubeSync email test via SMTP2GO API."

    payload = {
        "api_key": api_key,
        "to": [to_email],
        "sender": from_email,
        "subject": subject,
        "text_body": body,
    }

    print(f"→ POST {api_url} (sender={from_email} to={to_email})")
    try:
        r = requests.post(api_url, json=payload, timeout=30)
        print("← HTTP", r.status_code)
        print(r.text)
        if r.status_code != 200:
            sys.exit(6)
    except Exception as e:
        print(f"❌ SMTP2GO API ERROR — {e}", file=sys.stderr)
        sys.exit(6)


def test_smtp(cfg: ConfigParser) -> None:
    host      = cfg.get("email", "smtp_host", fallback="localhost")
    port      = cfg.getint("email", "smtp_port", fallback=25)
    use_tls   = cfg.getboolean("email", "use_tls", fallback=False)
    use_ssl   = cfg.getboolean("email", "use_ssl", fallback=False)
    username  = cfg.get("email", "username", fallback=None)
    password  = cfg.get("email", "password", fallback=None)
    from_email= cfg.get("email", "from_email")
    to_email  = cfg.get("email", "to_email")
    prefix    = cfg.get("email", "subject_prefix", fallback="[TubeSync] ")

    subject = prefix + " Test email"
    body    = "This is a TubeSync email test via SMTP."

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = from_email
    msg["To"]      = to_email
    msg["Date"]    = formatdate(localtime=True)

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=40)
        else:
            server = smtplib.SMTP(host, port, timeout=40)
            server.set_debuglevel(1)  # verboso
        if use_tls and not use_ssl:
            server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(from_email, [to_email], msg.as_string())
        server.quit()
        print("✅ SMTP OK")
    except Exception as e:
        print(f"❌ SMTP ERROR — {e}", file=sys.stderr)
        sys.exit(6)


def main():
    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} config.ini", file=sys.stderr)
        sys.exit(1)

    cfg = load_cfg(sys.argv[1])
    if not cfg.getboolean("email", "enabled", fallback=False):
        print("⚠️  [email].enabled=false — nessun invio eseguito.")
        sys.exit(0)

    method = cfg.get("email", "method", fallback="smtp").strip().lower()

    if method == "sendgrid":
        test_sendgrid(cfg)
    elif method == "smtp2go_api":
        test_smtp2go_api(cfg)
    elif method == "smtp":
        test_smtp(cfg)
    else:
        print(f"❌ Metodo email non supportato: {method} (usa 'sendgrid', 'smtp2go_api' o 'smtp')", file=sys.stderr)
        sys.exit(7)


if __name__ == "__main__":
    main()
