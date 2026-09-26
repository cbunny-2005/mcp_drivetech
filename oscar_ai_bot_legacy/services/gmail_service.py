"""
Gmail service — SINGLE shared account, authenticated ONCE before deploy.

Unlike the per-user OAuth sketch in gmail.md, the RFQ pipeline monitors ONE
mailbox. Credentials are a long-lived refresh token supplied via env
(GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN) — minted once with
scripts/gmail_oauth_bootstrap.py and pasted into .env, so the running service
never prompts and survives Render redeploys (data/ is wiped; env is not).

This is the ONLY module that talks to Gmail. It is read-oriented: list/parse
unread, detect attachments, apply the RFQ-Processed label, mark read. No send.

Scope: gmail.modify (read + modify labels / mark read).
"""

import base64
import logging
import os
import threading

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROCESSED_LABEL = os.getenv("RFQ_PROCESSED_LABEL", "RFQ-Processed")

_service = None
_service_lock = threading.Lock()
_label_id_cache: dict[str, str] = {}


# ── Auth / service singleton ────────────────────────────────────────────────
def _build_service():
    """Lazy thread-safe Gmail API client from the env refresh token.
    Mirrors firebase_service._get_app()'s double-checked-lock pattern."""
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is not None:
            return _service

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_id     = os.getenv("GMAIL_CLIENT_ID", "")
        client_secret = os.getenv("GMAIL_CLIENT_SECRET", "")
        refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "")
        if not (client_id and client_secret and refresh_token):
            raise EnvironmentError(
                "Gmail not configured — set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, "
                "GMAIL_REFRESH_TOKEN in .env (run scripts/gmail_oauth_bootstrap.py once)."
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        # cache_discovery=False avoids a noisy warning + file cache on read-only FS.
        _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.info("[GMAIL] service initialised (single-account)")
        return _service


def is_configured() -> bool:
    return bool(
        os.getenv("GMAIL_CLIENT_ID")
        and os.getenv("GMAIL_CLIENT_SECRET")
        and os.getenv("GMAIL_REFRESH_TOKEN")
    )


# ── Fetch ───────────────────────────────────────────────────────────────────
def list_unread(query: str = "is:unread", max_results: int = 10) -> list[str]:
    """Return message ids matching a Gmail search query (newest first)."""
    svc = _build_service()
    resp = (
        svc.users().messages()
        .list(userId="me", q=query, maxResults=min(max_results, 50))
        .execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def get_message(message_id: str) -> dict:
    """Fetch and parse one message → normalized dict:
    {id, thread_id, from_email, from_name, subject, date, body, snippet,
     has_attachments}."""
    svc = _build_service()
    msg = (
        svc.users().messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    from_raw = headers.get("from", "")
    from_name, from_email = _split_from(from_raw)

    body, has_attachments = _extract_body_and_attachments(payload)
    return {
        "id":              msg["id"],
        "thread_id":       msg.get("threadId"),
        "from_email":      from_email,
        "from_name":       from_name,
        "subject":         headers.get("subject", "(no subject)"),
        "date":            headers.get("date", ""),
        "body":            body,
        "snippet":         msg.get("snippet", ""),
        "has_attachments": has_attachments,
    }


# ── Modify ──────────────────────────────────────────────────────────────────
def ensure_label(name: str = PROCESSED_LABEL) -> str:
    """Return the label id for `name`, creating it once if missing. Cached."""
    if name in _label_id_cache:
        return _label_id_cache[name]
    svc = _build_service()
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl["name"].lower() == name.lower():
            _label_id_cache[name] = lbl["id"]
            return lbl["id"]
    created = (
        svc.users().labels()
        .create(userId="me", body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        })
        .execute()
    )
    _label_id_cache[name] = created["id"]
    logger.info("[GMAIL] created label %s (%s)", name, created["id"])
    return created["id"]


def mark_processed(message_id: str, mark_read: bool = True) -> None:
    """Apply the RFQ-Processed label (durable dedup signal) and optionally remove
    UNREAD. The label — not read-state — is the authoritative 'processed' marker."""
    svc = _build_service()
    add = [ensure_label(PROCESSED_LABEL)]
    remove = ["UNREAD"] if mark_read else []
    svc.users().messages().modify(
        userId="me", id=message_id,
        body={"addLabelIds": add, "removeLabelIds": remove},
    ).execute()


# ── Parsing helpers ─────────────────────────────────────────────────────────
def _split_from(raw: str) -> tuple[str, str]:
    """'John Smith <john@x.com>' → ('John Smith', 'john@x.com')."""
    from email.utils import parseaddr
    name, addr = parseaddr(raw)
    return (name or None), (addr or None)


def _extract_body_and_attachments(payload: dict) -> tuple[str, bool]:
    """Walk MIME parts. Prefer text/plain; fall back to a stripped text/html.
    Any part with a filename counts as an attachment (flagged, not downloaded)."""
    plain, html, has_attachments = [], [], False

    def walk(part):
        nonlocal has_attachments
        filename = part.get("filename")
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if filename:  # any named part = attachment
            has_attachments = True
        if mime == "text/plain" and data:
            plain.append(_decode(data))
        elif mime == "text/html" and data:
            html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    elif html:
        text = _strip_html("\n".join(html))
    else:
        text = ""
    return text.strip(), has_attachments


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text
