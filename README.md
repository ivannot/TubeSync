# TubeSync — YouTube auto uploader per Synology

Automatizza l’upload dei video su YouTube quando vengono copiati nelle cartelle sorgente del tuo NAS Synology.  
Funziona con **OAuth YouTube Data API v3**, tiene traccia dei file già caricati, invia **email di esito**, scrive i log nel **Log Center** di DSM e gestisce automaticamente lo **stop** quando si supera la **quota** delle API.

## Caratteristiche
- Monitoraggio in tempo reale delle cartelle (watcher con debounce configurabile).
- Upload **resumable** con progress, privacy, categoria e descrizione da config.
- **Deduplica**: non ricarica file invariati; opzionale idratazione da playlist YouTube.
- **Gestione quota**: pausa automatica (configurabile) se superi limiti API/upload.
- Notifiche **email** via:
  - SMTP classico (STARTTLS/SSL)
  - **SendGrid** (API key)
  - **SMTP2GO** (API key)
- Log nativi in **Log Center** tramite `synologset1`.
- Nessun path hard-coded: tutto via `config.ini`.
- **Rescan periodico** (es. ogni ora) anche senza eventi FS.
- Script ausiliari:  
  - `mail_test.py` → test configurazione email  
  - `state_db_tool.py` → lista/esporta stato `state.db`

---

## Requisiti

- Synology DSM 7.x con:
  - **Log Center** installato
  - (Opz.) Mail Server locale se vuoi SMTP interno
- **Python 3.8+** sul NAS
- Un computer con browser (Mac/PC) per la **prima autenticazione OAuth**
- Un canale YouTube abilitato a caricare video

---

## Configurazione Google / YouTube API

1. Vai su **Google Cloud Console** → crea un **Project** (es. “TubeSync”).
2. Abilita **YouTube Data API v3**.
3. Configura l’**OAuth consent screen** (user type = External, scopes `youtube.upload`, `youtube.readonly`, test user = il tuo account Google).
4. Crea credenziali → **OAuth client ID → Desktop app**.
5. Scarica il JSON → rinominalo `client_secret.json`.

⚠️ Non committare mai `client_secret.json` o `token.json`.

---

## Configurazione (`config.ini`)

Esempio completo:

```ini
[general]
client_secret_path = client_secret.json
token_path         = token.json
db_path    = state.db
log_path   = tubesync.log
pause_file = .pause_until
quota_cooldown_minutes = 1440   # default: 24h di pausa

source_dirs = /volume2/video/Volo/Originali, /volume2/video/Volo
allowed_extensions = .mp4, .mov, .m4v, .avi, .mkv

privacy      = private
category_id  = 22
description  =
made_for_kids = false

use_sha1                = false
skip_if_smaller_than_mb = 5

chunk_mb    = 8
max_retries = 8

hydrate_from_youtube_on_start = true
hydrate_match = exact_title

[email]
enabled   = true

# Metodo: smtp | sendgrid | smtp2go
method    = sendgrid
sendgrid_api_key = <YOUR_KEY>
smtp2go_api_key  = <YOUR_KEY>

# Per smtp classico
smtp_host = smtp.office365.com
smtp_port = 587
use_tls   = true
use_ssl   = false
username  = user@example.com
password  = password

from_email     = ivan@notaristefano.com
to_email       = ivan@notaristefano.com
subject_prefix = [TubeSync]

[watcher]
debounce_seconds = 90
rescan_minutes   = 60    # run periodico (0 = disattivato)
pause_check_seconds = 30 # ogni quanto verifica scadenza pausa
```

---

## Email di test

```bash
source .venv/bin/activate
python3 mail_test.py config.ini
```

Invia una mail statica "TubeSync test email" e mostra log dettagliati.

---

## State DB tool

```bash
python3 state_db_tool.py list
python3 state_db_tool.py export
```

Serve a visualizzare o esportare in CSV il contenuto di `state.db`.

---

## Logging

- Eventi watcher (`created`, `moved`)
- Debounce loggato solo a livello **DEBUG**
- Run iniziale e rescan periodici → **INFO**
- Log in Log Center (`TubeSyncWatcher`, `TubeSyncUploader`)
- Opzioni extra:
  - `TS_CONSOLE=1` → duplica log su console
  - `TS_FILE=/path/file.log` → duplica log su file

---

## Requirements

`requirements.txt`:

```
google-api-python-client
google-auth-httplib2
google-auth-oauthlib
requests
watchdog
sendgrid
```

---

## Struttura repo

```
TubeSync/
├─ tubesync_synology.py
├─ tubesync_watcher.py
├─ mail_test.py
├─ state_db_tool.py
├─ config.ini.example
├─ requirements.txt
└─ README.md
```

---

## Troubleshooting

- **Non vedo log in Log Center**  
  Test:
  ```bash
  /usr/syno/bin/synologset1 sys info 0x11100000 "TubeSync TEST"
  ```
- **QuotaExceeded**: pausa automatica, controlla `.pause_until`.
- **Email non arriva**: usa `mail_test.py` per verificare credenziali e metodo.
- **Token scaduto**: elimina `token.json` e rifai login con `client_secret.json`.

---

## Licenza
MIT (consigliata).  
Escludi i file segreti (`client_secret.json`, `token.json`, `state.db`, `.pause_until`) dal repo.
