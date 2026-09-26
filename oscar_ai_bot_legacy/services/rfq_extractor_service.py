"""
RFQ extractor — pulls structured customer + product data from an RFQ email.

A single gpt-4o-mini JSON call. Customer identity is grounded in the Gmail
headers (passed in) so the model doesn't hallucinate the sender; products,
quantities, units and notes come from the body. The model must NEVER invent
prices or part numbers — those are resolved later by price_lookup_service.

Returns a validated RfqExtraction. Mirrors tools/contact_tools.py's OpenAI usage.
"""

import json
import logging

from openai import OpenAI
from schemas.pydantic_schemas import RfqExtraction, RfqProduct

logger = logging.getLogger(__name__)
_client = OpenAI()

_SYSTEM = """You extract structured data from a Request For Quotation (RFQ) email.

Return ONLY JSON with this exact shape:
{
  "customer_name": string|null,     // the person who sent it
  "company": string|null,
  "email": string|null,
  "address": string|null,           // buyer's postal address, if stated
  "gstin": string|null,             // buyer's GSTIN, if stated
  "pan": string|null,               // buyer's PAN, if stated
  "phone": string|null,             // buyer's phone, if stated
  "subject": string|null,
  "description": string|null,       // one-line summary of the overall ask
  "products": [
    {"product": string, "quantity": number|null, "unit": string|null,
     "size": string|null, "brand": string|null, "notes": string|null}
  ],
  "extraction_confidence": 0.0-1.0
}

RULES:
- NEVER invent prices, part numbers, or products not present in the email.
- Missing values MUST be null — do NOT guess.
- Keep each product's "product" text close to how the customer wrote it.
- If the email is a forwarded RFQ, extract the ORIGINAL requester from the quoted
  headers, not the internal forwarder.
- "customer_name"/"company"/"email" describe the SENDER/requester. Prefer the
  header values provided; only override from the body if clearly more correct.
- "address"/"gstin"/"pan"/"phone" are the BUYER's details — extract them ONLY if
  they actually appear in the email (often in a signature or letterhead). If not
  present, return null. NEVER guess or fabricate a GSTIN/PAN/address."""


def extract(subject: str, body: str, from_email: str = "",
            from_name: str = "") -> RfqExtraction:
    """Extract customer + products from an RFQ email into a validated RfqExtraction."""
    header_ctx = (
        f"[Header ground-truth] From name: {from_name or '(unknown)'} | "
        f"From email: {from_email or '(unknown)'} | Subject: {subject or ''}"
    )
    content = f"{header_ctx}\n\nBody:\n{(body or '')[:6000]}"
    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.error("[RFQ] extract failed: %s", exc)
        # Minimal fallback so the pipeline can still create a review task.
        return RfqExtraction(
            company=from_name or None, email=from_email or None,
            subject=subject or None, products=[], extraction_confidence=0.0,
        )

    products = []
    for p in data.get("products") or []:
        try:
            products.append(RfqProduct(
                product=str(p.get("product", "")).strip(),
                quantity=_num(p.get("quantity")),
                unit=_s(p.get("unit")),
                size=_s(p.get("size")),
                brand=_s(p.get("brand")),
                notes=_s(p.get("notes")),
            ))
        except Exception:
            continue
    # Header values win for identity unless the model clearly filled them.
    return RfqExtraction(
        customer_name=_s(data.get("customer_name")) or (from_name or None),
        company=_s(data.get("company")),
        email=_s(data.get("email")) or (from_email or None),
        address=_s(data.get("address")),
        gstin=_s(data.get("gstin")),
        pan=_s(data.get("pan")),
        phone=_s(data.get("phone")),
        subject=_s(data.get("subject")) or (subject or None),
        description=_s(data.get("description")),
        products=[p for p in products if p.product],
        extraction_confidence=float(data.get("extraction_confidence", 0.0) or 0.0),
    )


def _s(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
