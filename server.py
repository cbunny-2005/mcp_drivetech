"""
Quotations MCP Server (standalone)
==================================

A Model Context Protocol (MCP) server that exposes the **quotations-app backend**
as tools an MCP agent can call. It is a thin **HTTP client** over the quotations
REST API — it holds no database of its own, so it stays fully decoupled and can
point at any deployment (local dev or the live Render backend).

    MCP agent ──MCP──► THIS server ──HTTP──► quotations-app backend ──► MongoDB

Deployed separately from everything else. Later, the Oscar agent connects to this
server (as an MCP client, via a new endpoint) to create/read/reassign quotations.

Backend API it wraps (see backend/API_CONTRACT.md):
    GET   /api/quotations                    → list all
    GET   /api/quotations/{id}               → get one
    POST  /api/quotations/generate           → create (all fields optional)
    PATCH /api/quotations/{id}/assignee      → set assignee name

Config (env)
------------
    QUOTATIONS_API_URL   base URL of the quotations backend
                         (default: https://quotations-app.onrender.com)
    QUOTATIONS_TIMEOUT   HTTP timeout seconds (default 30 — Render free tier can
                         cold-start ~50s, so bump for the first call if needed)
    MCP_TRANSPORT        stdio (default) | http   (http → endpoint at /mcp)
    MCP_HOST / MCP_PORT  bind for http transport (default 127.0.0.1:8200)

Install
-------
    pip install "mcp[cli]" httpx

Run
---
    python mcp_server/server.py                                   # stdio
    MCP_TRANSPORT=http MCP_PORT=8200 python mcp_server/server.py  # → http://host:8200/mcp
"""

from __future__ import annotations

import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("quotations-mcp")
logging.basicConfig(level=os.getenv("MCP_LOG_LEVEL", "INFO"))

API_URL = os.getenv("QUOTATIONS_API_URL", "https://quotations-app.onrender.com").rstrip("/")
TIMEOUT = float(os.getenv("QUOTATIONS_TIMEOUT", "30"))

mcp = FastMCP(
    name="quotations-mcp",
    instructions=(
        "READ-ONLY conversational interface over the quotations data. Use these "
        "tools to ANSWER questions — counts, totals, who-has-what, look-ups — never "
        "to create or modify anything (this server does not write). Prices/totals "
        "come only from the server-computed data; never invent them. A quotation is "
        "'completed' only if its stored `status` says so — if every status is "
        "'draft', report that nothing is marked complete rather than guessing."
    ),
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    # MCP_PORT wins locally; fall back to $PORT (Render/Heroku set this) then 8200.
    port=int(os.getenv("MCP_PORT") or os.getenv("PORT") or "8200"),
)


def _api() -> str:
    return API_URL


def _get(path: str):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{_api()}{path}")
        r.raise_for_status()
        return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# TOOLS
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def health() -> dict:
    """Liveness + which backend this MCP server points at. Read-only.
    Also confirms the backend is reachable (it wakes a sleeping Render instance)."""
    try:
        n = len(_get("/api/quotations"))
        return {"server": "quotations-mcp", "backend": _api(), "reachable": True,
                "quotation_count": n}
    except Exception as e:
        return {"server": "quotations-mcp", "backend": _api(), "reachable": False,
                "error": str(e)}


@mcp.tool()
def business_context() -> dict:
    """Business profile for THIS MCP/org. The client shows `short` in the info box,
    `details` under 'Know more', and loads `system_instructions` into the agent so
    it behaves as this business's assistant. Each MCP owns its own profile → new
    business = new MCP, no client change."""
    return {
        "business": "Drive Tech Engineering",
        "short": ("Drive Tech Engineering — industrial SS pipes, fittings & valves. "
                  "Ask me about your quotations: totals, who owns what, or a "
                  "specific customer."),
        "details": ("I'm Drive Tech Engineering's quotations assistant. I can look up "
                    "any quotation, tell you the total pipeline value, show what's "
                    "assigned to whom, and pull up a customer's or quotation-number's "
                    "details. Prices come only from the price list — never invented."),
        "capabilities": [
            "Look up any quotation by number or company",
            "Total pipeline value & counts",
            "See who a quotation is assigned to",
        ],
        "getting_started": [
            "Ask: what's the total pipeline value?",
            "Ask: show quotations for <company>",
        ],
        "system_instructions": (
            "You are Drive Tech Engineering's quotations assistant. Drive Tech "
            "supplies industrial stainless-steel pipes, fittings, valves and gaskets. "
            "Help with quotation lookups, totals, and assignments. Prices/part "
            "numbers come only from the data — never invent them."
        ),
    }


@mcp.tool()
def suggested_questions() -> dict:
    """DATA-DRIVEN starter questions — built at call time from the ACTUAL quotation
    data (real company names, assignees, statuses, counts), so the chips reflect
    what's really in the system right now. Read-only. Falls back to a couple of
    generic prompts if there's no data yet."""
    try:
        rows = _get("/api/quotations") or []
    except Exception:
        rows = []
    if not rows:
        return {"suggestions": [
            "How many quotations do we have?",
            "What's the total pipeline value?",
        ]}

    companies, assignees, statuses = [], [], {}
    for q in rows:
        c = (q.get("to") or {}).get("companyName")
        if c and c not in companies:
            companies.append(c)
        a = (q.get("assignee") or {}).get("name")
        if a and a not in assignees:
            assignees.append(a)
        s = q.get("status") or "draft"
        statuses[s] = statuses.get(s, 0) + 1

    sugg = [
        "What's the total pipeline value?",
        f"How many quotations do we have? ({len(rows)})",
    ]
    if len(assignees) > 1:
        sugg.append("Who has the most quotations?")
    # Real company names → tailored chips
    for c in companies[:2]:
        sugg.append(f"Show quotations for {c}")
    # Real assignee → tailored chip
    if assignees:
        sugg.append(f"Which quotations are assigned to {assignees[0]}?")
    # Most common status → tailored chip
    if statuses:
        top = max(statuses, key=statuses.get)
        sugg.append(f"List all {top} quotations ({statuses[top]})")

    # de-dupe, keep order, cap
    seen, out = set(), []
    for s in sugg:
        if s not in seen:
            seen.add(s); out.append(s)
    return {"suggestions": out[:8]}


@mcp.tool()
def list_quotations() -> dict:
    """List all quotations (summary fields). Read-only.
    Returns id, number, date, status, assignee, customer, and total for each."""
    rows = _get("/api/quotations")
    out = [{
        "quotationId": q.get("quotationId"),
        "quotationNumber": q.get("quotationNumber"),
        "quotationDate": q.get("quotationDate"),
        "status": q.get("status"),
        "assignee": (q.get("assignee") or {}).get("name"),
        "customer": (q.get("to") or {}).get("companyName"),
        "total": (q.get("summary") or {}).get("total"),
    } for q in (rows or [])]
    return {"count": len(out), "quotations": out}


@mcp.tool()
def get_quotation(quotation_id: str) -> dict:
    """Fetch ONE full quotation by its `quotationId` (e.g. 'qt_...'). Read-only.
    Returns the complete object (from/to, items, summary, terms) or a not-found note."""
    try:
        return {"found": True, "quotation": _get(f"/api/quotations/{quotation_id}")}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"found": False, "quotation_id": quotation_id}
        raise


# ── Conversational-intelligence tools (query + analytics over the full set) ──
# The backend only exposes list + get, so filtering/aggregation happens here so
# an agent can answer natural-language questions without pulling everything each
# time and reasoning over raw rows.

def _all_full() -> list[dict]:
    return _get("/api/quotations") or []


def _summ(q: dict) -> dict:
    """Compact, answer-friendly view of one quotation."""
    return {
        "quotationId": q.get("quotationId"),
        "quotationNumber": q.get("quotationNumber"),
        "quotationDate": q.get("quotationDate"),
        "status": q.get("status"),
        "assignee": (q.get("assignee") or {}).get("name"),
        "customer": (q.get("to") or {}).get("companyName"),
        "supplier": (q.get("from") or {}).get("companyName"),
        "total": (q.get("summary") or {}).get("total"),
        "item_count": len(q.get("items") or []),
    }


@mcp.tool()
def search_quotations(
    company: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    number: str | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    limit: int = 25,
) -> dict:
    """Search/filter quotations for a conversational answer. All filters optional
    and combined with AND; text filters are case-insensitive substring matches.
    Read-only.

    Args:
        company:   customer company name contains this.
        status:    exact status (e.g. 'draft').
        assignee:  assignee name contains this (e.g. 'Vijender').
        number:    quotation number contains this (e.g. 'QT-2026-0000').
        min_total / max_total: bound the quotation total (₹).
        limit:     max rows returned, newest first (default 25).
    """
    rows = _all_full()

    def ok(q: dict) -> bool:
        to_name = ((q.get("to") or {}).get("companyName") or "").lower()
        asg = ((q.get("assignee") or {}).get("name") or "").lower()
        num = (q.get("quotationNumber") or "").lower()
        tot = (q.get("summary") or {}).get("total") or 0
        if company and company.lower() not in to_name:
            return False
        if status and status.lower() != (q.get("status") or "").lower():
            return False
        if assignee and assignee.lower() not in asg:
            return False
        if number and number.lower() not in num:
            return False
        if min_total is not None and tot < min_total:
            return False
        if max_total is not None and tot > max_total:
            return False
        return True

    hits = [q for q in rows if ok(q)]
    hits.sort(key=lambda q: q.get("quotationNumber") or "", reverse=True)
    return {
        "matched": len(hits),
        "total_value": round(sum((q.get("summary") or {}).get("total") or 0 for q in hits), 2),
        "quotations": [_summ(q) for q in hits[:limit]],
    }


@mcp.tool()
def find_by_number(quotation_number: str) -> dict:
    """Resolve a human quotation number (e.g. 'QT-2026-000005') to the FULL
    quotation. Read-only. Use when the user cites a quotation number rather than
    an internal id."""
    for q in _all_full():
        if (q.get("quotationNumber") or "").lower() == quotation_number.strip().lower():
            return {"found": True, "quotation": q}
    return {"found": False, "quotation_number": quotation_number}


@mcp.tool()
def quotation_stats() -> dict:
    """Aggregate stats for conversational answers: total count, breakdown by
    status, total pipeline value, and count + value grouped by assignee. Read-only.
    Use for questions like 'how many are pending?', 'total value?', 'who has the
    most quotations?'."""
    rows = _all_full()
    by_status: dict[str, int] = {}
    by_assignee: dict[str, dict] = {}
    total_value = 0.0
    for q in rows:
        st = q.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        tot = (q.get("summary") or {}).get("total") or 0
        total_value += tot
        asg = (q.get("assignee") or {}).get("name") or "Unassigned"
        slot = by_assignee.setdefault(asg, {"count": 0, "value": 0.0})
        slot["count"] += 1
        slot["value"] = round(slot["value"] + tot, 2)
    return {
        "total_quotations": len(rows),
        "total_pipeline_value": round(total_value, 2),
        "by_status": by_status,
        "by_assignee": by_assignee,
    }


# NOTE: This MCP is a READ-ONLY interface over the customer's data — it queries
# and answers, it never writes. Creating/reassigning quotations is the customer
# app's job, not ours. (A `status`/completion answer is therefore only possible
# if the customer's DB actually stores that state — today it hard-codes
# status="draft", so `quotation_stats().by_status` will only ever show what the
# data holds. The moment their app writes a real status, these read tools surface
# it automatically — no change here.)


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    logger.info("quotations-mcp → backend %s (transport=%s)", _api(), transport)
    if transport == "http":
        mcp.run(transport="streamable-http")   # endpoint at host:port/mcp
    else:
        mcp.run()                              # stdio


if __name__ == "__main__":
    main()
