"""
ONE-TIME Gmail OAuth bootstrap for the single RFQ mailbox.

Run this LOCALLY once, on the machine where you can open a browser and sign in
as the mailbox account. It performs the installed-app OAuth flow, then prints the
three values to paste into .env so the deployed backend authenticates silently
forever (via the refresh token — no further prompts, survives Render redeploys).

Usage:
    # provide the GCP OAuth *desktop* client id/secret (from Google Cloud Console)
    export GMAIL_CLIENT_ID=xxxxx.apps.googleusercontent.com
    export GMAIL_CLIENT_SECRET=GOCSPX-xxxxx
    .venv/bin/python scripts/gmail_oauth_bootstrap.py

A browser window opens → approve access for the mailbox account → the script
prints GMAIL_REFRESH_TOKEN (and echoes the client id/secret) for your .env.

Scope: gmail.modify (read + modify labels / mark read). No send, no delete.
"""

import os
import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _write_env(updates: dict) -> None:
    """Update (or append) keys in the project .env, in place."""
    from pathlib import Path
    env_path = Path(__file__).resolve().parent.parent / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        # Replace either a live "KEY=" or a commented "# KEY=" placeholder.
        matched = False
        for key in list(remaining.keys()):
            if stripped == f"# {key}=" or stripped.startswith(f"{key}="):
                out.append(f"{key}={remaining.pop(key)}")
                matched = True
                break
        if not matched:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    env_path.write_text("\n".join(out) + "\n")


def main() -> int:
    # Load .env so the client id/secret can live there (no manual export needed).
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("ERROR: set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET env vars first.")
        print("Get them from Google Cloud Console → APIs & Services → Credentials")
        print("→ create an OAuth client of type 'Desktop app'.")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib not installed. Run:")
        print("  .venv/bin/pip install google-auth-oauthlib")
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

    # Force Google Chrome (not the macOS default browser, e.g. Safari) so the
    # consent screen opens where the user asked. Falls back silently if Chrome
    # isn't registered — the printed URL can always be opened manually.
    import webbrowser
    if sys.platform == "darwin":
        try:
            webbrowser.register(
                "chrome", None,
                webbrowser.MacOSXOSAScript("Google Chrome"), preferred=True,
            )
            print("→ Will open the consent screen in Google Chrome.")
        except Exception as _e:
            print(f"(could not force Chrome: {_e} — using default browser)")

    # access_type=offline + prompt=consent guarantees a refresh_token is returned.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent",
        authorization_prompt_message=(
            "If Chrome didn't open, paste this URL into Google Chrome:\n\n{url}\n"
        ),
        success_message="Authorized. You can close this tab and return to the terminal.",
    )

    if not creds.refresh_token:
        print("\nERROR: no refresh_token returned. Revoke prior access at")
        print("https://myaccount.google.com/permissions and re-run.")
        return 1

    # Write straight into .env so the running service is configured immediately.
    try:
        _write_env({
            "GMAIL_CLIENT_ID": client_id,
            "GMAIL_CLIENT_SECRET": client_secret,
            "GMAIL_REFRESH_TOKEN": creds.refresh_token,
        })
        wrote = True
    except Exception as e:
        wrote = False
        print(f"(could not auto-write .env: {e})")

    print("\n" + "=" * 70)
    print("SUCCESS — Gmail authorized." + (" .env updated automatically." if wrote else ""))
    print("=" * 70)
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70)
    if wrote:
        print("Written to .env — nothing to copy. You can return to Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
