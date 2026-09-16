#!/usr/bin/env python3
"""
youtube_auth.py — one-time YouTube OAuth setup.

Before running this you need a Google Cloud OAuth client:

  1. https://console.cloud.google.com/  → create (or pick) a project.
  2. APIs & Services → Library → enable "YouTube Data API v3".
  3. APIs & Services → OAuth consent screen:
       - User type: External
       - Fill in app name / support email / developer email
       - Scopes: add  .../auth/youtube.upload
       - Test users: add the Google account that owns the channel.
         (While the app is in "Testing", only listed test users can authorise,
         and refresh tokens expire after 7 days. Publishing the app keeps them
         alive — an unverified app is fine for a personal channel.)
  4. APIs & Services → Credentials → Create credentials → OAuth client ID
       - Application type: **Desktop app** — NOT "Web application".
         A web client only accepts redirect URIs you register by hand, so the
         local server this script starts is rejected with
         "Error 400: redirect_uri_mismatch". A desktop client accepts any
         localhost port, which is exactly what this flow needs.
       - Download the JSON, save it next to this script as client_secret.json

Then:

    python3 youtube_auth.py

A browser window opens; approve, and token.json is written here. agent.py
refreshes that token automatically from then on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Authorise YouTube uploads.")
    ap.add_argument("--client-secret", default=str(ROOT / "client_secret.json"))
    ap.add_argument("--token", default=str(ROOT / "token.json"))
    ap.add_argument("--port", type=int, default=8080,
                    help="local redirect port (0 = pick a free one)")
    ap.add_argument("--force", action="store_true",
                    help="re-authorise even if token.json already works")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        sys.exit(
            "Missing deps. Run:\n"
            "  pip install google-auth-oauthlib google-api-python-client google-auth-httplib2"
        )

    token_path = Path(args.token)
    secret_path = Path(args.client_secret)

    # Reuse / refresh an existing token unless --force.
    if token_path.exists() and not args.force:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.valid:
            print(f"{token_path.name} is already valid — nothing to do.")
            return 0
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json())
                print(f"Refreshed {token_path.name}.")
                return 0
            except Exception as exc:  # noqa: BLE001
                print(f"Refresh failed ({exc}) — running the full flow again.")

    if not secret_path.exists():
        sys.exit(
            f"{secret_path} not found.\n"
            "Download your Desktop-app OAuth client JSON from Google Cloud Console\n"
            "(APIs & Services → Credentials) and save it there. See this file's\n"
            "docstring for the full walkthrough."
        )

    # Catch the wrong client type before opening a browser. A "Web application"
    # client stores its config under "web" and only accepts redirect URIs that
    # were registered by hand — so the loopback server this flow starts gets
    # rejected with Error 400: redirect_uri_mismatch. A "Desktop app" client
    # stores its config under "installed" and accepts any localhost port.
    try:
        client_type = next(iter(json.loads(secret_path.read_text())))
    except (json.JSONDecodeError, StopIteration):
        sys.exit(f"{secret_path} is not valid OAuth client JSON — re-download it.")

    if client_type == "web":
        sys.exit(
            f"{secret_path.name} is a WEB APPLICATION client, but this flow needs a\n"
            "DESKTOP APP client. That mismatch is what causes:\n"
            "    Error 400: redirect_uri_mismatch\n\n"
            "Fix it either way:\n\n"
            "  A. Make a Desktop client (recommended, no URI bookkeeping)\n"
            "     Google Cloud Console → APIs & Services → Credentials\n"
            "     → Create credentials → OAuth client ID\n"
            "     → Application type: Desktop app\n"
            "     → Download the JSON and replace this file.\n\n"
            "  B. Keep this web client and register the redirect URI\n"
            "     Open the client → Authorised redirect URIs → ADD:\n"
            f"        http://localhost:{args.port}/\n"
            "     The trailing slash matters, and the port must match\n"
            "     (this script uses --port, default 8080).\n"
        )
    if client_type != "installed":
        print(f"Warning: unexpected client type {client_type!r} — expected 'installed'.")

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    creds = flow.run_local_server(
        port=args.port,
        prompt="consent",          # forces a refresh_token to be issued
        access_type="offline",
        authorization_prompt_message="Opening your browser to authorise…",
        success_message="Authorised. You can close this tab and return to the terminal.",
    )
    token_path.write_text(creds.to_json())
    print(f"Wrote {token_path}")
    print("Keep this file private — it can upload to your channel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
