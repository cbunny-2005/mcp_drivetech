"""
Quotation client — builds the quotation payload and submits it to the external
Quotation Application. This is the ONLY seam between Oscar and that app.

Design contract (so swapping the mock for the real API touches ONLY this file):
  - build_payload(extraction, matched_lines) -> dict   (deterministic; no LLM)
  - submit(payload) -> {quotationId, quotationNumber, editableUrl}
  - create_quotation(extraction, matched_lines) -> (payload, response)

Config (all via env, nothing hardcoded):
  QUOTATION_API_URL     base URL of the Quotation App (unset -> mock mode)
  QUOTATION_MOCK=1      force mock even if a URL is set (for testing)
  QUOTATION_API_KEY     optional; auth is pluggable and encapsulated here
  QUOTATION_AUTH_SCHEME 'bearer' (default) | 'apikey-header' | 'none'
  QUOTATION_FROM_*      static supplier ("from") block (companyName/contact/...)

Pricing rules (locked): rate = List Price, discount = 0, and Oscar computes every
total deterministically (qty x rate). The LLM never touches money.
"""

import logging
import os
import uuid

import requests

logger = logging.getLogger(__name__)

_TERMS = [
    "Prices are exclusive of GST.",
    "Payment Terms: 100% Advance.",
    "Delivery: As mentioned against each item.",
    "Freight: Extra at actuals.",
    "Quotation Validity: 30 Days.",
]


# ── Static supplier block ("from") ──────────────────────────────────────────
def _supplier_block() -> dict:
    """The seller identity — constant, from env. Fill these in .env before go-live."""
    return {
        "companyName":   os.getenv("QUOTATION_FROM_COMPANY", "Our Company Pvt. Ltd."),
        "contactPerson": os.getenv("QUOTATION_FROM_CONTACT", ""),
        "email":         os.getenv("QUOTATION_FROM_EMAIL", ""),
        "phone":         os.getenv("QUOTATION_FROM_PHONE", ""),
        "address":       os.getenv("QUOTATION_FROM_ADDRESS", ""),
        "gstin":         os.getenv("QUOTATION_FROM_GSTIN", ""),
        "pan":           os.getenv("QUOTATION_FROM_PAN", ""),
    }


# ── Payload construction (deterministic) ────────────────────────────────────
def build_payload(extraction, matched_lines: list[dict]) -> dict:
    """Assemble the Quotation App request payload. EVERY item the client asked
    for is included so the full request is represented: matched lines carry the
    real price/part# from the price list; not-found lines carry only the asked
    name + qty (price/part# left blank for manual fill — never invented)."""
    items, sub_total, total_qty = [], 0.0, 0.0
    line_no = 0
    for m in matched_lines:
        line_no += 1
        qty = float(m.get("quantity") or 0)
        total_qty += qty
        # Item shape matches the Quotation App's model (seed_quotation.py /
        # quotationMath.js): itemName / unitPrice / discountPercentage / delivery.
        if m.get("matched") and m.get("rate") is not None:
            rate = float(m["rate"])
            sub_total += round(qty * rate, 2)     # discount = 0 for MVP
            items.append({
                "lineNumber":         line_no,
                "partNumber":         m.get("part_number"),
                "itemName":           m.get("description") or m.get("requested"),
                "delivery":           m.get("lead_time") or "Ready Stock",
                "quantity":           qty,
                "unitPrice":          rate,
                "discountPercentage": 0,
            })
        else:
            # Not found in the price lists — include the asked item so the client's
            # full order is on the quotation; price/part# blank for a human to fill.
            items.append({
                "lineNumber":         line_no,
                "partNumber":         "",
                "itemName":           m.get("requested"),
                "delivery":           "",
                "quantity":           qty,
                "unitPrice":          0,
                "discountPercentage": 0,
            })

    sub_total = round(sub_total, 2)
    to_block = {
        # Fall back to the sender's email when no company name was extracted, so
        # "Quotation For" always shows *something* identifying the requester
        # rather than a blank.
        "companyName": (getattr(extraction, "company", None)
                        or getattr(extraction, "email", None)),
        "address":     getattr(extraction, "address", None),
        "gstin":       getattr(extraction, "gstin", None),
        "pan":         getattr(extraction, "pan", None),
        "email":       getattr(extraction, "email", None),
        "phone":       getattr(extraction, "phone", None),
    }
    # Payload keys match the Quotation App's create_quotation() contract.
    return {
        "currency": "INR",
        "from": _supplier_block(),
        "to": to_block,
        "items": items,
        "summary": {
            "totalQuantity":      total_qty,
            "subTotal":           sub_total,
            "discountPercentage": 0,
            "discountAmount":     0.0,
            "total":              sub_total,     # == subTotal since discount = 0
        },
        "termsAndConditions": _TERMS,
        "remarks": "Auto-generated from RFQ email — review before sending.",
    }


# ── Submission (real or mock) ───────────────────────────────────────────────
def _is_mock() -> bool:
    return os.getenv("QUOTATION_MOCK") == "1" or not os.getenv("QUOTATION_API_URL")


def _auth_headers() -> dict:
    """Pluggable auth — encapsulated so the pipeline never knows the scheme.
    MVP default: no auth. Add QUOTATION_API_KEY + scheme later without touching
    any caller."""
    key = os.getenv("QUOTATION_API_KEY", "")
    scheme = os.getenv("QUOTATION_AUTH_SCHEME", "bearer").lower()
    if not key or scheme == "none":
        return {}
    if scheme == "apikey-header":
        return {"X-API-Key": key}
    return {"Authorization": f"Bearer {key}"}


def submit(payload: dict) -> dict:
    """POST the payload to the Quotation App. Returns {quotationId,
    quotationNumber, editableUrl}. Falls back to a mock in mock mode.

    No auto-retry: a read timeout is not a confirmed failure, and retrying could
    create a duplicate quotation (same stance as the WhatsApp send path)."""
    if _is_mock():
        return _mock_response(payload)

    url = os.getenv("QUOTATION_API_URL").rstrip("/") + "/api/quotations/generate"
    timeout = float(os.getenv("QUOTATION_TIMEOUT_SEC", "30"))
    headers = {"Content-Type": "application/json", **_auth_headers()}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    qid = data.get("quotationId")
    return {
        "quotationId":     qid,
        "quotationNumber": data.get("quotationNumber"),
        # The app returns `editable` (bool), not a URL — build the deep link to
        # the frontend detail route (/quotations/:id) ourselves.
        "editableUrl":     data.get("editableUrl") or _editable_url(qid),
    }


def _editable_url(quotation_id: str) -> str:
    """Deep link into the Quotation App frontend's detail/edit page."""
    base = os.getenv("QUOTATION_FRONTEND_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/quotations/{quotation_id}"


def update_assignee(quotation_id: str, assignee_name: str) -> bool:
    """Sync the quotation's assignee name to the Quotation App (PATCH
    /api/quotations/{id}/assignee). Used when the linked Oscar task is
    reassigned. No-op in mock mode. Returns True on success."""
    if not quotation_id or _is_mock():
        return False
    url = (os.getenv("QUOTATION_API_URL").rstrip("/")
           + f"/api/quotations/{quotation_id}/assignee")
    timeout = float(os.getenv("QUOTATION_TIMEOUT_SEC", "30"))
    headers = {"Content-Type": "application/json", **_auth_headers()}
    resp = requests.patch(url, json={"assigneeName": assignee_name},
                          headers=headers, timeout=timeout)
    resp.raise_for_status()
    return True


def _mock_response(payload: dict) -> dict:
    """Deterministic-shape mock matching the agreed response contract."""
    qid = f"qt_{uuid.uuid4().hex[:8]}"
    logger.info("[QUOTATION] MOCK submit — %d item(s), subTotal=%s",
                len(payload.get("items", [])), payload.get("summary", {}).get("subTotal"))
    return {
        "quotationId":     qid,
        "quotationNumber": f"QT-2026-{uuid.uuid4().int % 1000000:06d}",
        "editableUrl":     _editable_url(qid),
    }


def create_quotation(extraction, matched_lines: list[dict]) -> tuple[dict, dict]:
    """Convenience: build + submit in one call. Returns (payload, response)."""
    payload = build_payload(extraction, matched_lines)
    if not payload["items"]:
        raise ValueError("no priced line items — nothing to quote")
    response = submit(payload)
    return payload, response
