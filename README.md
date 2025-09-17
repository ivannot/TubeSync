# TubeSync — YouTube auto uploader per Synology

Automatizza l’upload dei video su YouTube quando vengono copiati nelle cartelle sorgente del tuo NAS Synology.  
Funziona con **OAuth YouTube Data API v3**, tiene traccia dei file già caricati, invia **email di esito**, scrive i log nel **Log Center** di DSM e gestisce automaticamente lo **stop** quando si supera la **quota** delle API.

## Caratteristiche
- Monitoraggio in tempo reale delle cartelle (watcher, debounce configurabile).
- Upload **resumable** con progress, privacy, categoria e descrizione da config.
- **Deduplica**: non ricarica file invariati; opzionale lettura titoli già presenti nel canale.
- **Gestione quota**: al primo `quotaExceeded` va in pausa per N minuti e interrompe il run.
- Notifiche **email** (SMTP/STARTTLS/SSL), subject personalizzato.
- Log nativi in **Log Center** tramite `synologset1`.
- Nessun path hard-coded: tutto via `config.ini`.

---

## Requisiti

- Synology DSM 7.x con:
  - **Log Center** installato (per visualizzare i log).
  - (Opzionale) **Synology Mail Server** o altro SMTP per l’invio email.
- **Python 3.8+** sul NAS (pacchetto “Python3” di Synology).
- Un computer con browser (Mac/PC) per fare **la prima autenticazione OAuth** e generare `token.json`.
- Un canale YouTube abilitato a caricare video.

---

## 1) Configurazione Google Cloud / YouTube API

1. Vai su **Google Cloud Console** → crea un **Project** (es. “TubeSync”).
2. **Enable APIs & Services** → cerca e abilita **YouTube Data API v3**.
3. **OAuth consent screen**:
   - User type: **External** (va bene anche “Testing”).
   - App name: es. `TubeSync`.
   - User support email: la tua.
   - **Scopes**: aggiungi  
     - `.../auth/youtube.upload`  
     - `.../auth/youtube.readonly`
   - **Test users**: aggiungi il tuo account Google che userà l’app.
   - Salva (non serve pubblicare l’app: “Testing” è ok).
4. **Credentials** → **Create Credentials** → **OAuth client ID**:
   - Application type: **Desktop app**
   - Name: `TubeSync Desktop`
   - Scarica il JSON → rinominalo **`client_secret.json`**.

> ⚠️ Non committare mai `client_secret.json` e `token.json` su GitHub.

---

## 2) Preparazione cartella sul NAS

```bash
# crea cartella di lavoro
mkdir -p /volume2/TubeSync
cd /volume2/TubeSync

# copia qui gli script del progetto + client_secret.json
# (tubesync_synology.py, tubesync_watcher.py, config.ini, ecc.)

# crea venv
python3 -m venv .venv
source .venv/bin/activate

# dipendenze
pip install --upgrade pip
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib watchdog
```

---

## 3) Configurazione (`config.ini`)

Esempio completo (adatta i percorsi):

```ini
[general]
client_secret_path = /volume2/TubeSync/client_secret.json
token_path        = /volume2/TubeSync/token.json

db_path   = /volume2/TubeSync/state.db
log_path  = /volume2/TubeSync/tubesync.log
pause_file = /volume2/TubeSync/.pause_until
quota_cooldown_minutes = 120

source_dirs = /volume2/video/Volo/Originali, /volume2/video/Volo
allowed_extensions = .mp4, .mov, .m4v, .avi, .mkv

privacy      = private
category_id  = 22
description  =

use_sha1                 = false
skip_if_smaller_than_mb  = 5

chunk_mb    = 8
max_retries = 8

hydrate_from_youtube_on_start = true
hydrate_match = exact_title

[email]
enabled  = true
smtp_host = 127.0.0.1
smtp_port = 587
use_tls   = true
use_ssl   = false
username = ivan
password = ********

from_email    = ivan@nas.local
to_email      = ivan@notaristefano.com
subject_prefix = [TubeSync]

[watcher]
debounce_seconds = 90

[paths]
uploader_path = /volume2/TubeSync/tubesync_synology.py
```

---

## 4) Prima autenticazione (creazione `token.json`)

Il NAS non ha un browser: fai l’autenticazione su un Mac/PC.

1. Copia `client_secret.json` e `config.ini` sul Mac (stessa struttura).
2. Crea venv e installa le stesse dipendenze del NAS.
3. Esegui (dal Mac):
   ```bash
   python3 /percorso/tubesync_synology.py /percorso/config.ini
   ```
   Si aprirà il browser: consenti gli scope **youtube.upload** e **youtube.readonly**.
4. Verrà creato `token.json` (stessa cartella del `client_secret.json`).
5. Copia **`token.json`** nel NAS: `/volume2/TubeSync/token.json`.

> D’ora in poi l’upload potrà avvenire dal NAS senza ulteriori interventi.

---

## 5) Test di funzionamento

Esegui una scansione/upload **una tantum**:

```bash
source /volume2/TubeSync/.venv/bin/activate
python3 /volume2/TubeSync/tubesync_synology.py /volume2/TubeSync/config.ini
```

- Lo script scansiona le cartelle `source_dirs`.
- Carica i file idonei e invia email di esito.
- Log visibili in **Log Center → Logs → System** (cerca “TubeSync”).
- Se compare `quotaExceeded`, lo script crea `pause_file` e **termina**: niente spam di email; i run successivi attendono la scadenza.

---

## 6) Avvio automatico: Watcher + Task Scheduler

### Avvio manuale (debug)
```bash
source /volume2/TubeSync/.venv/bin/activate
nohup python3 /volume2/TubeSync/tubesync_watcher.py /volume2/TubeSync/config.ini >/dev/null 2>&1 &
pgrep -af tubesync_watcher.py
```

### Task Scheduler (DSM 7 – English UI)
- **Control Panel → Task Scheduler → Create → Triggered Task → User-defined script**
- **General**:
  - Task: `TubeSync Watcher`
  - User: un utente con permessi sulla cartella (es. `ivan`)
  - Enabled: ✓
- **Schedule**: `Run on boot-up`
- **Task Settings → Run command**:
  ```sh
  /bin/sh -c 'source /volume2/TubeSync/.venv/bin/activate && nohup python3 /volume2/TubeSync/tubesync_watcher.py /volume2/TubeSync/config.ini >/dev/null 2>&1 &'
  ```
- Salva.

> Il watcher esegue subito una **scansione iniziale**, poi resta in ascolto.  
> Debounce (default 90s) evita run ripetuti mentre i file vengono copiati.

---

## 7) Logging su Log Center

Gli script usano il comando nativo:
```
/usr/syno/bin/synologset1 sys <level> 0x11100000 "<Program>: <message>"
```
quindi i messaggi appaiono in **Log Center → Logs → System** con:
- Program = `TubeSyncWatcher` / `TubeSyncUploader`
- Level = Info/Warning/Error

### Test rapido
```bash
/usr/syno/bin/synologset1 sys info 0x11100000 "TubeSync Uploader: manual test $(date)"
```

### Opzioni di debug (facoltative)
- `TS_FILE=/volume2/TubeSync/debug.log` → duplica i log su file
- `TS_CONSOLE=1` → duplica i log su console (utile se lanci in foreground)

---

## 8) Email di notifica

Compila la sezione `[email]` del `config.ini`.  
Con **Synology Mail Server** locale tipicamente:
- `smtp_host=127.0.0.1`, `smtp_port=587`, `use_tls=true`
- `username`/`password`: utente DSM locale
- `from_email`: es. `ivan@nas.local`
- `to_email`: tua casella reale

Gli errori (incluso `quotaExceeded`) inviano una **sola email** per run; alla prima quota l’uploader crea `pause_file` e si ferma.

---

## 9) Come funziona la deduplica

- Tabella `uploads` in `state.db` salva: path, size, mtime, (opz.) sha1, status.
- Se `status=done` e il file non è cambiato (size/mtime/sha1), viene **skippato**.
- Con `hydrate_from_youtube_on_start=true`, all’avvio legge la playlist “Uploads” del canale e marca come `done` i file il cui **titolo** combacia (case-insensitive) con `stem` del file (senza estensione).

---

## 10) Quota & pausa

- Alla prima risposta **403/429** con motivo `quotaExceeded`/`userRateLimitExceeded`/`dailyLimitExceeded`:
  - crea `pause_file` con scadenza = `now + quota_cooldown_minutes`
  - manda **una sola email**
  - **termina** il run (gli altri file non partono).
- I run successivi, se `pause_file` non è scaduto, escono subito (silenziosamente nei log).

---

## 11) Manutenzione & consigli

- Aggiorna dipendenze:
  ```bash
  source /volume2/TubeSync/.venv/bin/activate
  pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib watchdog
  ```
- Per forzare il **ri-upload** di un file: elimina la riga corrispondente da `uploads` o cambia nome/timestamp del file.
- Se cambi **account YouTube**: elimina `token.json` e riesegui l’autenticazione.
- Tieni d’occhio **Log Center** per eventuali errori di rete o credenziali.

---

## 12) Struttura consigliata del repo

```
TubeSync/
├─ tubesync_synology.py
├─ tubesync_watcher.py
├─ config.ini.example
├─ requirements.txt
└─ README.md
```

### `requirements.txt`
```
google-api-python-client
google-auth-httplib2
google-auth-oauthlib
watchdog
```

### `.gitignore` (fondamentale)
```
# Secrets & runtime
client_secret.json
token.json
state.db
*.log
debug.log
.pause_until

# Python
.venv/
__pycache__/
*.pyc
```

---

## 13) Troubleshooting

- **Non vedo log in Log Center**  
  Usa il test nativo:
  ```bash
  /usr/syno/bin/synologset1 sys info 0x11100000 "TubeSync TEST"
  ```
  Se lo vedi, gli script loggheranno correttamente (usano lo stesso meccanismo).

- **403 insufficientPermissions** all’idratazione  
  Rigenera `token.json` assicurandoti di concedere **entrambi gli scope** (upload + readonly).

- **Email non arriva**  
  Prova un test SMTP con lo script che hai già usato; verifica porta, TLS/SSL e credenziali.

- **Upload lento/interrotto**  
  Aumenta `chunk_mb` (es. 16/32) o riducilo se vedi timeouts. La velocità dipende dalla banda del NAS.

---

## Licenza
Scegli tu (MIT consigliata). Ricordati di escludere i **segreti** (`client_secret.json`/`token.json`) dal repo.
