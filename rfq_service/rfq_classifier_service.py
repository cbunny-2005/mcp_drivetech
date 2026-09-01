"""
RFQ classifier — decides whether an inbound email is a Request For Quotation.

Two stages, cheap-then-smart:
  1. Deterministic prefilter — kills obvious non-RFQs (auto-replies, newsletters,
     no-reply senders) before spending an LLM call; flags obvious RFQ keywords.
  2. LLM classification — a single gpt-4o-mini JSON call for anything ambiguous.

Per MVP policy there is NO confidence gating — every detected RFQ is processed and
the Team Lead reviews the generated quotation. Confidence is stored for audit only.
"""

import json
import logging
import os

from openai import OpenAI
from rfq_schemas import RfqClassification

logger = logging.getLogger(__name__)
_client = None


def _get_client() -> OpenAI:
    """Lazy init — constructing OpenAI() at import time would crash the whole
    worker process before its own error handling ever runs if the API key is
    missing/blank."""
    global _client
    if _client is None:
        _client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SEC", "20")))
    return _client

# Strong negative signals — if present, almost certainly not an RFQ to quote.
_NEGATIVE = (
    "no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon",
    "unsubscribe", "newsletter", "out of office", "automatic reply",
    "delivery status notification", "undeliverable",
)
# Positive RFQ signals.
_POSITIVE = (
    "rfq", "request for quotation", "request for quote", "quotation", "quote",
    "pricing", "price list", "enquiry", "inquiry", "please quote", "kindly quote",
    "boq", "bill of quantity", "tender", "proforma", "requirement",
)

_SYSTEM = """You classify whether an email is a Request For Quotation (RFQ) — a \
customer asking a supplier to quote a price for specific products or materials.

Return ONLY JSON: {"is_rfq": true|false, "confidence": 0.0-1.0, "reason": "<short>"}.

RFQ examples: "Please send your best price for 20 SS elbows", "We need a quotation \
for the attached BOQ", "Kindly quote for 50 gaskets, size 25mm".
NOT RFQ: marketing/newsletters, order confirmations, invoices, generic questions \
with no product/quantity ask, internal chatter, meeting invites.
Judge by intent, not just keywords. A forwarded RFQ is still an RFQ."""


def _prefilter(subject: str, body: str, from_email: str) -> str:
    """Return 'reject' | 'accept' | 'unsure'."""
    blob = f"{from_email}\n{subject}\n{body}".lower()
    if any(sig in blob for sig in _NEGATIVE):
        return "reject"
    if any(sig in f"{subject} {body}".lower() for sig in _POSITIVE):
        return "accept"
    return "unsure"


def classify(subject: str, body: str, from_email: str = "") -> RfqClassification:
    """Classify an email. Runs the prefilter, then the LLM for accept/unsure."""
    stage = _prefilter(subject or "", body or "", from_email or "")
    if stage == "reject":
        return RfqClassification(is_rfq=False, confidence=0.95,
                                 reason="prefilter: negative signal (auto/bulk/no-reply)")

    # Both 'accept' and 'unsure' go to the LLM — keyword presence alone is not proof.
    content = f"Subject: {subject}\n\nFrom: {from_email}\n\nBody:\n{(body or '')[:4000]}"
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        return RfqClassification(
            is_rfq=bool(data.get("is_rfq")),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            reason=(data.get("reason") or "")[:300],
        )
    except Exception as exc:
        logger.error("[RFQ] classify failed: %s", exc)
        # Fail toward the keyword prefilter's opinion (accept → likely RFQ).
        likely = stage == "accept"
        return RfqClassification(is_rfq=likely, confidence=0.3,
                                 reason=f"llm_error; prefilter={stage}")
