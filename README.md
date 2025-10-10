# TubeSync — YouTube auto uploader per Synology

Automatizza l'upload dei video su YouTube quando vengono copiati nelle cartelle sorgente del tuo NAS Synology.  
Funziona con **OAuth YouTube Data API v3**, tiene traccia dei file già caricati, invia **email di esito**, scrive i log nel **Log Center** di DSM e gestisce automaticamente lo **stop** quando si supera la **quota** delle API.

## Caratteristiche
- Monitoraggio in tempo reale delle cartelle (watcher con debounce configurabile).
- Upload **resumable** con progress, privacy, categoria e descrizione da config.
- **Deduplica**: non ricarica file invariati; opzionale idratazione da playlist YouTube.
- **Gestione quota**: pausa automatica (configurabile) se superi limiti API/upload.
- **Gestione errori critici**: sospende automaticamente le esecuzioni in caso di problemi (es. token scaduto) per evitare spam di email.
- Notifiche **email** via:
  - SMTP classico (STARTTLS/SSL)
  - **SendGrid** (API key)
  - **SMTP2GO** (API key)
- Log nativi in **Log Center** tramite `synologset1`.
- Nessun path hard-coded: tutto via `config.ini`.
- **Rescan periodico** (es. ogni ora) anche senza eventi FS.
- Script ausiliari:  
  - `tubesync.sh` → gestione servizio watcher (start/stop/restart/status)
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

1. Vai su **Google Cloud Console** → crea un **Project** (es. "TubeSync").
2. Abilita **YouTube Data API v3**.
3. Configura l'**OAuth consent screen** (user type = External, scopes `youtube.upload`, `youtube.readonly`, test user = il tuo account Google).
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

send_summary = true
send_summary_when_noop = false

[watcher]
debounce_seconds = 90
settle_seconds = 60
max_debounce_seconds = 900
rescan_minutes   = 60    # run periodico (0 = disattivato)
pause_check_seconds = 30 # ogni quanto verifica scadenza pausa
event_log_interval_seconds = 180
```

---

## Script di gestione (`tubesync.sh`)

Lo script `tubesync.sh` gestisce il servizio watcher:

```bash
./tubesync.sh start         # Avvia il watcher (rimuove lock errori)
./tubesync.sh stop          # Ferma il watcher
./tubesync.sh restart       # Riavvia (utile dopo aver corretto errori)
./tubesync.sh status        # Mostra stato e eventuali errori critici
./tubesync.sh clear-error   # Rimuove lock errori senza restart
```

### Gestione errori critici

Quando si verifica un **errore critico** (es. token YouTube scaduto):

1. 📧 Ricevi **UNA SOLA email** con dettagli e istruzioni
2. 🔒 Il sistema crea `/volume2/TubeSync/.error_lock`
3. ⏸️ Tutte le esecuzioni automatiche vengono **sospese**
4. 📝 Log Center mostra `[TubeSync:SUSPENDED]`

**Il watcher continua a girare** ma NON eseguirà l'uploader finché non risolvi e fai `restart`.

**Per risolvere:**
```bash
# 1. Correggi il problema (es. rigenera token.json)
# 2. Riavvia il servizio
./tubesync.sh restart
```

**Verifica stato:**
```bash
./tubesync.sh status
# Se c'è un errore attivo, vedrai il messaggio con istruzioni
```

Questo previene lo **spam di email** - ricevi solo 1 notifica per errore critico.

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

### Log Center (Synology)

I log appaiono in **Log Center → Sistema** con tag `[TubeSync:*]`:

**Da tubesync_watcher.py:**
- `[TubeSync:START]` - Avvio watcher
- `[TubeSync:INIT]` - Scansione iniziale
- `[TubeSync:WATCH]` - Cartelle monitorate
- `[TubeSync:RUNNING]` - Watcher attivo
- `[TubeSync:EVENT]` - File system events (created/modified/moved)
- `[TubeSync:UPLOAD]` - Esecuzione uploader
- `[TubeSync:RESCAN]` - Rescan periodico
- `[TubeSync:ACTIVITY]` - Statistiche attività
- `[TubeSync:SUSPENDED]` - Esecuzioni sospese per errore critico

**Da tubesync_synology.py:**
- `[TubeSync:START]` - Avvio script
- `[TubeSync:AUTH]` - Autenticazione YouTube
- `[TubeSync:AUTH_FAIL]` - Errore autenticazione
- `[TubeSync:HYDRATE]` - Idratazione da YouTube
- `[TubeSync:SUCCESS]` - Upload completato
- `[TubeSync:SKIP]` - File già presente
- `[TubeSync:ERROR]` - Errori
- `[TubeSync:RETRY]` - Tentativi di retry
- `[TubeSync:SUMMARY]` - Riepilogo esecuzione

**Da tubesync.sh:**
- `[TubeSync:SCRIPT]` - Start/stop servizio

### Filtro Log Center

Nel Log Center cerca "**TubeSync**" per vedere solo i tuoi log.

### Livelli di log

- Eventi watcher (`created`, `moved`)
- Debounce loggato solo a livello **DEBUG**
- Run iniziale e rescan periodici → **INFO**
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
├─ tubesync_synology.py      # Script upload YouTube
├─ tubesync_watcher.py        # Watcher file system
├─ tubesync.sh                # Script gestione servizio ⭐
├─ mail_test.py               # Test configurazione email
├─ state_db_tool.py           # Tool per state.db
├─ config.ini.example         # Template configurazione
├─ requirements.txt
├─ README.md
│
├─ .venv/                     # Virtual environment Python
├─ client_secret.json         # ⚠️ NON committare
├─ token.json                 # ⚠️ NON committare
├─ state.db                   # ⚠️ NON committare
├─ .pause_until               # ⚠️ NON committare
├─ .error_lock                # ⚠️ NON committare (gestione errori)
└─ tubesync_watcher.pid       # ⚠️ NON committare
```

---

## Troubleshooting

### Non vedo log in Log Center

Test manuale:
```bash
synologset1 sys info 0x11100000 "[TubeSync:TEST] Test manuale log"
```
Controlla **Log Center → Sistema** e cerca "TubeSync".

### Token YouTube scaduto

**Sintomo:** Email "ERRORE CRITICO - Autenticazione fallita", status mostra "Esecuzioni sospese"

**Soluzione:**
```bash
# 1. Elimina il token scaduto
rm token.json

# 2. Rigenera il token (esegui da Mac/PC con browser)
python3 -c "from google_auth_oauthlib.flow import InstalledAppFlow; \
flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', \
['https://www.googleapis.com/auth/youtube.upload', \
'https://www.googleapis.com/auth/youtube.readonly']); \
creds = flow.run_local_server(port=0); \
import json; \
open('token.json', 'w').write(json.dumps({'token': creds.token, \
'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, \
'client_id': creds.client_id, 'client_secret': creds.client_secret, \
'scopes': creds.scopes}))"

# 3. Copia token.json sul NAS
# 4. Riavvia il servizio
./tubesync.sh restart
```

Il sistema **NON invierà altre email** di errore finché non riavvii.

### QuotaExceeded

Pausa automatica attiva. Controlla:
```bash
cat .pause_until
# Mostra fino a quando è in pausa

# Per forzare la ripresa (sconsigliato):
rm .pause_until
./tubesync.sh restart
```

### Watcher sospeso per errore critico

```bash
# Verifica il problema
./tubesync.sh status

# Dopo aver risolto
./tubesync.sh restart

# Oppure rimuovi solo il lock
./tubesync.sh clear-error
```

### Email non arriva

Test configurazione:
```bash
source .venv/bin/activate
python3 mail_test.py config.ini
```
Verifica credenziali e metodo (smtp/sendgrid/smtp2go).

### Watcher non rileva nuovi file

Verifica:
```bash
# Il watcher è attivo?
./tubesync.sh status

# Estensioni corrette in config.ini?
grep allowed_extensions config.ini

# Test manuale
touch /volume2/video/test.mp4
# Controlla i log dopo ~90 secondi (debounce)
```

---

## File automatici da escludere

Aggiungi al `.gitignore`:

```
# Credenziali e token
client_secret.json
token.json

# Database e stato
state.db
*.db-journal

# File di controllo
.pause_until
.error_lock
tubesync_watcher.pid

# Log locali
*.log

# Python
.venv/
__pycache__/
*.pyc
```

---

## Licenza

MIT
