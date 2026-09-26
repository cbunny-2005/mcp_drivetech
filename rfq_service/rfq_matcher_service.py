"""
RFQ matcher — the AGENT that prepares quotation lines from requested products.

Design intent (per product owner): the AGENT owns matching. price_lookup_service
is a pure TOOL that only *serves candidate rows* from the loaded price lists — it
never decides and never talks to the agent by itself. This service is the agent:
for each requested product it asks the lookup tool for candidates (real rows with
real prices) and then REASONS about which candidate is the right one, handling
fuzzy/ambiguous cases with judgment.

HARD GUARDRAIL: the agent may only SELECT among the real candidates returned by
the tool. The price and part number always come from the chosen Excel row — the
LLM never invents a price, a part number, or a product.

Fast path: when the tool returns a confident exact match, we accept it without an
LLM call (no judgment needed). The agent is invoked only for the ambiguous cases.
"""

import json
import logging
import os

from openai import OpenAI
from rfq_schemas import RfqProduct
import embedding_service
import price_lookup_service

logger = logging.getLogger(__name__)
_client = None


def _get_client() -> OpenAI:
    """Lazy init — see rfq_classifier_service._get_client() for why."""
    global _client
    if _client is None:
        _client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SEC", "20")))
    return _client

_SYSTEM = """You match a customer's requested product to a supplier's industrial \
price list (pumps, valves, gaskets, seals, heat-exchanger plates, fittings). You \
are given the request and a numbered list of CANDIDATE rows (real description + price).

Return ONLY JSON: {"choice": <candidate_number or -1>, "confidence": 0.0-1.0, "review": true|false, "reason": "<short>"}.

THREE OUTCOMES:
- WRONG TYPE (candidate is a different kind of part) → choice = -1 (no match).
- RIGHT TYPE but you are NOT fully sure it is the exact item (generic candidate
  name, unclear size, more than one plausible option) → PICK IT and set "review": true.
  Keep it in the quote but flag it for a human to verify.
- RIGHT TYPE and confident it is the correct item → pick it, "review": false.

A wrong-TYPE match at the wrong price is far worse than "no match", so for wrong
type always return -1. But do NOT reject a same-type item just because you are
unsure which exact one — pick it and flag review=true instead.

DIFFERENT PART TYPES — these are NEVER a match, even if a word overlaps:
- a FERRULE is NOT a "ferrule HOUSING / HSG / assembly" (the housing holds ferrules).
- a FERRULE / CLAMP / GASKET is NOT an "IMPELLER", "SHAFT", "SEAL", or "PLATE".
- a BEND / ELBOW is NOT a "PLATE" or "SUPPORT".
- a single item is NOT a "KIT / SET / ASSEMBLY" unless the request asked for one.
- Watch price sanity: a small fitting (ferrule, clamp, gasket) costing lakhs is a
  red flag that you matched a large assembly by mistake — reject it.

SAME PART TYPE — allowed to differ in wording/size/grade (SELECT these):
- Abbreviations: "CH PL"/"CH PLATE" = channel plate; "SS" = stainless; "GKT" = gasket.
- Sizes: "25mm" ≈ "DN25", "40mm" ≈ "DN40". Prefer the candidate with the matching size.
- Grades 316/316L, materials, finishes — minor differences are fine.
- A model/type code in the request (e.g. "T8-M2", "DR10") appearing in a candidate
  description IS a strong, valid match.

PROCESS:
1. Identify the core noun of the request (ferrule? clamp? gasket? plate? bend?).
2. Keep only candidates that ARE that same part type. Discard word-overlap look-alikes.
3. Among those, pick the best by size/grade/material. If none remain, return -1.
Never invent a product, part number, or price — choose only from the numbered candidates."""


def match_products(products: list[RfqProduct]) -> list[dict]:
    """Prepare quotation line items for a list of requested products.
    Returns one dict per product: matched flag, chosen row (part_number, price,
    description, lead_time), quantity/unit from the request, and match metadata."""
    lines = []
    for p in products:
        lines.append(_match_one(p))
    return lines


def _match_one(p: RfqProduct) -> dict:
    query = " ".join(x for x in [p.product, p.size, p.brand] if x)
    part_hint = _looks_like_part(p.product) or _looks_like_part(p.notes)
    size_token = embedding_service.extract_size_token(p.size or p.product)

    # Tool call: gather real candidates (wide net so the agent has options). The
    # tool only SERVES rows — the agent always makes the final decision (no
    # deterministic fast-path; the LLM is always in the loop by design).
    res = price_lookup_service.lookup(query, part_number=part_hint,
                                      size_token=size_token, top_k=8)
    candidates = list(res.get("candidates") or [])

    # Ensure an exact part/description hit is on the table as the top candidate.
    best = res.get("best")
    exact_hit = res.get("method") in ("exact_part", "exact_desc")
    if best and not any(c.get("part_number") == best.get("part_number")
                        and c.get("description") == best.get("description")
                        for c in candidates):
        candidates.insert(0, best)

    if not candidates:
        return _line(p, None, 0.0, "unmatched", "no_candidates")

    # Agent judgment over the real candidates — always runs.
    choice, conf, reason, review = _agent_pick(query, candidates, exact_hit)
    if choice is None or choice < 0 or choice >= len(candidates):
        return _line(p, None, 0.0, "unmatched", f"agent:{reason}")
    status = "review" if review else "agent"
    return _line(p, candidates[choice], conf, status, reason, review=review)


def _agent_pick(query: str, candidates: list[dict], exact_hit: bool = False):
    hint = "  <-- exact code/description match in price list" if exact_hit else ""
    listing = "\n".join(
        f"{i}. {c.get('description') or ''} | part={c.get('part_number')} "
        f"| price={c.get('price')} | file={c.get('source_file')}"
        f"{hint if i == 0 else ''}"
        for i, c in enumerate(candidates)
    )
    content = f"Requested product: {query}\n\nCandidates:\n{listing}"
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
        choice = data.get("choice")
        choice = int(choice) if choice is not None else -1
        return (choice, float(data.get("confidence", 0.0) or 0.0),
                (data.get("reason") or "")[:200], bool(data.get("review", False)))
    except Exception as exc:
        logger.error("[RFQ] agent match failed: %s", exc)
        # Deterministic fallback: take the top fuzzy candidate, flagged for review.
        return 0, float(candidates[0].get("_score", 0) or 0) / 100.0, "llm_error_fallback_top", True


def _line(p: RfqProduct, row, confidence, status, method, review=False) -> dict:
    """Assemble one quotation line. Price/part ALWAYS from the chosen row (or null)."""
    return {
        "requested": p.product,
        "quantity": p.quantity,
        "unit": p.unit,
        "size": p.size,
        "brand": p.brand,
        "notes": p.notes,
        "matched": row is not None,
        "review_needed": bool(review),    # matched but flagged ⚠️ for human check
        "status": status,                 # exact | agent | review | unmatched
        "match_method": method,
        "match_confidence": round(confidence, 3),
        "part_number": (row or {}).get("part_number"),
        "description": (row or {}).get("description"),
        "rate": (row or {}).get("price"),          # list price from Excel; never invented
        "lead_time": (row or {}).get("lead_time"),
        "source_file": (row or {}).get("source_file"),
    }


def _looks_like_part(s):
    """Heuristic: a token that looks like a supplier part/item code (long digit run)."""
    if not s:
        return None
    import re
    m = re.search(r"\b\d{6,}\b", str(s))
    return m.group(0) if m else None
