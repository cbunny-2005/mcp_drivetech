"""
One-time local script to mint a Gmail OAuth refresh token for this service.

Prerequisite (Google Cloud Console, done once): a Desktop-app OAuth Client ID
with the Gmail API enabled and gmail.modify added as a scope on the consent
screen (see rfq_service/.env.example for the full checklist). This script
just runs the browser consent flow with that client and prints the resulting
GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN to paste into .env
— it writes nothing to any file or database itself.

Usage:
    python gmail_oauth_bootstrap.py --client-id <id> --client-secret <secret>
    # or export GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET first and omit the flags
"""

import argparse
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default=os.getenv("GMAIL_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.getenv("GMAIL_CLIENT_SECRET"))
    args = ap.parse_args()

    if not args.client_id or not args.client_secret:
        raise SystemExit(
            "Need --client-id/--client-secret (or GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET "
            "env vars) from a Desktop-app OAuth client in Google Cloud Console.")

    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # Opens your browser for the consent screen; log in with the mailbox this
    # service should read, and accept. Nothing is written to disk here.
    creds = flow.run_local_server(port=0)

    print("\nSuccess — paste these into rfq_service/.env:\n")
    print(f"GMAIL_CLIENT_ID={args.client_id}")
    print(f"GMAIL_CLIENT_SECRET={args.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
