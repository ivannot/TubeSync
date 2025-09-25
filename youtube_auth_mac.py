#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
youtube_auth_mac.py
Forza il rilascio di un refresh token stabile (access_type=offline, prompt=consent)
e genera un token.json nella directory corrente.

Uso:
  python3 youtube_auth_mac.py
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

# Scopes richiesti da TubeSync
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

def main():
    here = Path(__file__).resolve().parent
    client_secret = here / "client_secret.json"
    if not client_secret.exists():
        raise FileNotFoundError(f"client_secret.json non trovato in: {client_secret}")

    # Forziamo sempre una nuova schermata di consenso e un refresh token
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    # run_local_server accetta extra params per Oauth:
    creds = flow.run_local_server(
        port=0,
        authorization_prompt_message="Autorizza TubeSync ad accedere a YouTube (upload + readonly).",
        success_message="✅ Autenticazione completata. Puoi chiudere questa finestra.",
        open_browser=True,
        access_type="offline",   # <-- fondamentale per avere il refresh_token
        prompt="consent",        # <-- forza nuova schermata consenso (rilascia refresh_token)
        include_granted_scopes="true",
    )

    token_path = here / "token.json"
    token_path.write_text(creds.to_json())
    print(f"✅ Nuovo token salvato in: {token_path}")

if __name__ == "__main__":
    main()
