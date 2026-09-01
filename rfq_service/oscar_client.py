"""
Oscar client — this service's ONLY seam into the Oscar backend. Mirrors
quotation_client.py's shape (one HTTP client file, deterministic contract, no
shared DB) but in the OPPOSITE direction: Oscar is external to THIS service,
the same way the Quotation App is external to Oscar.

Every call authenticates with a single shared secret (X-Internal-Secret),
matching Oscar's services/internal_api_router.py. No retry here for the same
reason quotation_client.py has none: a read timeout is not a confirmed
failure, and blind retries can double-create a task or double-notify.

Config (env):
    OSCAR_API_URL             base URL of the Oscar backend
                              (e.g. https://developement-branch.onrender.com)
    OSCAR_INTERNAL_SECRET     must match Oscar's INTERNAL_SERVICE_SECRET
    OSCAR_TIMEOUT_SEC         HTTP timeout seconds (default 30)
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


class OscarApiError(Exception):
    """Raised when Oscar's internal API returns a non-2xx response."""


def _base_url() -> str:
    url = os.getenv("OSCAR_API_URL", "").rstrip("/")
    if not url:
        raise EnvironmentError("OSCAR_API_URL is not set")
    return url


def _headers() -> dict:
    secret = os.getenv("OSCAR_INTERNAL_SECRET", "")
    if not secret:
        raise EnvironmentError("OSCAR_INTERNAL_SECRET is not set")
    return {"Content-Type": "application/json", "X-Internal-Secret": secret}


def _timeout() -> float:
    return float(os.getenv("OSCAR_TIMEOUT_SEC", "30"))


def _request(method: str, path: str, **kwargs) -> dict:
    url = f"{_base_url()}{path}"
    resp = requests.request(method, url, headers=_headers(),
                            timeout=_timeout(), **kwargs)
    if resp.status_code >= 400:
        raise OscarApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


# ── RFQ request audit/dedup rows ─────────────────────────────────────────────
def create_rfq_request(gmail_message_id: str, gmail_thread_id: str = None,
                       from_email: str = None, from_name: str = None,
                       subject: str = None, has_attachments: bool = False,
                       received_at: str = None) -> dict:
    """Dedup-insert one pa_rfq_requests row on Oscar. Returns the row (with
    `duplicate: true` if it already existed)."""
    return _request("POST", "/internal/rfq-requests", json={
        "gmail_message_id": gmail_message_id, "gmail_thread_id": gmail_thread_id,
        "from_email": from_email, "from_name": from_name, "subject": subject,
        "has_attachments": has_attachments, "received_at": received_at,
    })


def get_rfq_request(gmail_message_id: str) -> dict | None:
    """Dedup lookup. Returns None if never seen (404)."""
    try:
        return _request("GET", f"/internal/rfq-requests/{gmail_message_id}")
    except OscarApiError as e:
        if "-> 404" in str(e):
            return None
        raise


def patch_rfq_request(rfq_id: int, **fields) -> dict:
    """Update any subset of fields on one pa_rfq_requests row."""
    return _request("PATCH", f"/internal/rfq-requests/{rfq_id}", json=fields)


# ── Team lead resolution ─────────────────────────────────────────────────────
def get_team_lead(actor_user_id: int = None, fallback_user_id: int = None,
                  fallback_team_id: int = None) -> dict | None:
    """Returns {"user_id": int, "name": str} or None if nobody resolved (404)."""
    params = {}
    if actor_user_id:
        params["actor_user_id"] = actor_user_id
    if fallback_user_id:
        params["fallback_user_id"] = fallback_user_id
    if fallback_team_id:
        params["fallback_team_id"] = fallback_team_id
    try:
        return _request("GET", "/internal/team-lead", params=params)
    except OscarApiError as e:
        if "-> 404" in str(e):
            return None
        raise


# ── Task creation ─────────────────────────────────────────────────────────
def create_task(creator_user_id: int, title: str, assigned_to_user_id: int = None,
                description: str = None, priority: str = None,
                status: str = "pending", due_at: str = None) -> dict:
    """Create a task on Oscar. Returns {id, title, status, assigned_to_user_id, due_at}."""
    return _request("POST", "/internal/tasks", json={
        "creator_user_id": creator_user_id, "title": title,
        "assigned_to_user_id": assigned_to_user_id, "description": description,
        "priority": priority, "status": status, "due_at": due_at,
    })


# ── Notifications ────────────────────────────────────────────────────────────
def send_notification(user_id: int, notif_type: str, message: str,
                      item_id: int = None) -> None:
    _request("POST", "/internal/notifications", json={
        "user_id": user_id, "notif_type": notif_type, "message": message,
        "item_id": item_id,
    })


# ── Comments ──────────────────────────────────────────────────────────────
def post_task_comment(task_id: int, author_user_id: int, body: str,
                      role: str = "assistant", notify: bool = False) -> dict:
    return _request("POST", f"/internal/tasks/{task_id}/comments", json={
        "author_user_id": author_user_id, "body": body, "role": role, "notify": notify,
    })
