"""
Demo pipeline — thin wrapper around the real rfq_service modules, run
on-demand by the FastAPI backend. No DB: results live in an in-memory dict
for the lifetime of this process. TEMPORARY, for demoing the RFQ matching
pipeline only — delete the whole demo/ folder after the demo.

Reuses rfq_service's own .env (same OpenAI/Gmail/DATABASE_URL creds the real
worker uses) rather than duplicating secrets here.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

_RFQ_SERVICE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "rfq_service"))
sys.path.insert(0, _RFQ_SERVICE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_RFQ_SERVICE_DIR, ".env"))

import gmail_service
import quotation_client
import rfq_classifier_service
import rfq_extractor_service
import rfq_matcher_service

# Never let the demo call a real Quotation App or Oscar — mirrors dry_run_email.py.
os.environ["QUOTATION_MOCK"] = "1"

_RFQ_QUERY = "request for quotation OR quotation OR rfq OR enquiry"

_RUNS: dict[str, dict] = {}


def run_dry_run(max_results: int = 5) -> list[dict]:
    """Scan unread RFQ-looking emails right now and process each one.
    Returns the newly created run records (also cached in _RUNS)."""
    if not gmail_service.is_configured():
        raise RuntimeError("Gmail not configured — check rfq_service/.env")
    ids = gmail_service.list_unread(query=_RFQ_QUERY, max_results=max_results)
    created = []
    for mid in ids:
        email = gmail_service.get_message(mid)
        record = _process_one(email)
        _RUNS[record["id"]] = record
        created.append(record)
    return created


def _process_one(email: dict) -> dict:
    record = {
        "id": uuid.uuid4().hex[:12],
        "email_id": email["id"],
        "subject": email.get("subject"),
        "from_email": email.get("from_email"),
        "from_name": email.get("from_name"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": None,
        "extraction": None,
        "match_lines": None,
        "quotation": None,
    }

    cls = rfq_classifier_service.classify(
        email.get("subject", ""), email.get("body", ""), email.get("from_email", ""))
    record["classification"] = {
        "is_rfq": cls.is_rfq, "confidence": cls.confidence, "reason": cls.reason,
    }
    if not cls.is_rfq:
        return record

    ext = rfq_extractor_service.extract(
        email.get("subject", ""), email.get("body", ""),
        email.get("from_email", ""), email.get("from_name", ""))
    record["extraction"] = {
        "company": ext.company,
        "customer_name": ext.customer_name,
        "email": ext.email,
        "products": [p.model_dump() for p in ext.products],
    }

    lines = rfq_matcher_service.match_products(ext.products)
    record["match_lines"] = lines

    matched = [l for l in lines if l.get("matched")]
    if matched:
        record["quotation"] = quotation_client.build_payload(ext, lines)
    return record


def list_runs() -> list[dict]:
    return list(_RUNS.values())


def get_run(run_id: str) -> dict | None:
    return _RUNS.get(run_id)


def patch_item(run_id: str, line_number: int, fields: dict) -> dict | None:
    """Apply a partial update to one quotation line, then recompute the
    summary from ALL items server-side — never trust a stale summary."""
    record = _RUNS.get(run_id)
    if record is None or not record.get("quotation"):
        return None
    items = record["quotation"]["items"]
    item = next((i for i in items if i["lineNumber"] == line_number), None)
    if item is None:
        return None
    item.update({k: v for k, v in fields.items() if v is not None})
    record["quotation"]["summary"] = _recalc_summary(items)
    return record


def _recalc_summary(items: list[dict]) -> dict:
    total_qty = sum(float(i.get("quantity") or 0) for i in items)
    sub_total = 0.0
    for i in items:
        qty = float(i.get("quantity") or 0)
        rate = float(i.get("unitPrice") or 0)
        disc = float(i.get("discountPercentage") or 0)
        sub_total += qty * rate * (1 - disc / 100)
    sub_total = round(sub_total, 2)
    return {
        "totalQuantity": total_qty,
        "subTotal": sub_total,
        "discountPercentage": 0,
        "discountAmount": 0.0,
        "total": sub_total,
    }
