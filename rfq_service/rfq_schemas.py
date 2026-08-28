"""
RFQ pydantic schemas — moved verbatim from Oscar's schemas/pydantic_schemas.py
(RfqClassification, RfqProduct, RfqExtraction). Self-contained: no other Oscar
schema references these, and these reference nothing else in Oscar.
"""

from typing import List, Optional

from pydantic import BaseModel


class RfqClassification(BaseModel):
    """LLM output for RFQ detection. The LLM classifies only — no gating in MVP."""
    is_rfq: bool
    confidence: float = 0.0
    reason: Optional[str] = None


class RfqProduct(BaseModel):
    """A single requested product, extracted from the email body. The LLM must
    NEVER invent prices or part numbers — those come from the price list later."""
    product: str                       # raw product name as written in the email
    quantity: Optional[float] = None
    unit: Optional[str] = None          # Nos, kg, box, mtr...
    size: Optional[str] = None
    brand: Optional[str] = None
    notes: Optional[str] = None


class RfqExtraction(BaseModel):
    """LLM output for RFQ extraction. Customer identity is ground-truth from the
    Gmail headers; products/quantities/notes come from the body. No pricing."""
    customer_name: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    # Buyer's registration/contact details — extracted from the email ONLY when
    # present (never invented); flow into the quotation "to" block.
    address: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    phone: Optional[str] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    products: List[RfqProduct] = []
    extraction_confidence: float = 0.0
