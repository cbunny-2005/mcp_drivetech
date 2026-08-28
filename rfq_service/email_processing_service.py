"""
Email processing service — THE ORCHESTRATOR for the RFQ pipeline.

Coordinates every step for one inbound email and is the only place that wires the
single-responsibility services together (gmail, classifier, extractor, matcher,
quotation_client, oscar_client). Those services never call each other directly —
they go through here.

Flow:
  dedup -> create rfq-request row on Oscar -> classify -> extract -> match ->
  quotation_client.create_quotation -> create Oscar task (owner=Team Lead) ->
  notify lead + comment #1 -> update row -> (optionally) mark email processed.

This is the relocated version of Oscar's services/email_processing_service.py.
Every direct DB/ORM/service call has been replaced with an oscar_client call —
see oscar_client.py for the HTTP contract this now uses instead of a shared
SQLAlchemy session.
"""

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import oscar_client
import quotation_client
import rfq_classifier_service
import rfq_extractor_service
import rfq_matcher_service
from oscar_client import OscarApiError
from rfq_schemas import RfqExtraction

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def _now():
    return datetime.now(_IST).replace(tzinfo=None)


def resolve_team_lead(actor_user_id=None) -> dict | None:
    """Team Lead who owns the RFQ task, resolved via Oscar's /internal/team-lead
    (same fallback order as the in-process version: asker's own team lead ->
    RFQ_TEAM_LEAD_USER_ID -> RFQ_ORG_TEAM_ID's owner)."""
    fallback_user_id = os.getenv("RFQ_TEAM_LEAD_USER_ID")
    fallback_team_id = os.getenv("RFQ_ORG_TEAM_ID")
    return oscar_client.get_team_lead(
        actor_user_id=actor_user_id,
        fallback_user_id=int(fallback_user_id) if fallback_user_id else None,
        fallback_team_id=int(fallback_team_id) if fallback_team_id else None,
    )


def process_email(email: dict, mark_processed: bool = True,
                  create_task: bool = True, actor_user_id=None) -> dict:
    """Process one email end-to-end. `email` is a gmail_service.get_message() dict.
    Returns a summary dict. Idempotent on gmail_message_id.

    create_task=False → save the RFQ + generate the quotation but DON'T create an
    Oscar task/comment."""
    gmid = email["id"]

    # 1) Dedup — already seen. Return the STORED quotation so a repeat ask shows
    # it again instead of re-quoting or creating a duplicate task.
    existing = oscar_client.get_rfq_request(gmid)
    if existing:
        return _existing_summary(existing)

    rfq = oscar_client.create_rfq_request(
        gmail_message_id=gmid, gmail_thread_id=email.get("thread_id"),
        from_email=email.get("from_email"), from_name=email.get("from_name"),
        subject=(email.get("subject") or "")[:512],
        has_attachments=bool(email.get("has_attachments")),
        received_at=_now().isoformat(),
    )
    rfq_id = rfq["id"]

    try:
        # 2) Classify
        cls = rfq_classifier_service.classify(
            email.get("subject", ""), email.get("body", ""), email.get("from_email", ""))
        if not cls.is_rfq:
            oscar_client.patch_rfq_request(
                rfq_id, is_rfq=False, classify_confidence=str(cls.confidence),
                classify_reason=(cls.reason or "")[:1000], status="ignored")
            if mark_processed:
                _safe_mark(gmid)
            return {"status": "ignored", "rfq_id": rfq_id, "reason": cls.reason}

        oscar_client.patch_rfq_request(
            rfq_id, is_rfq=True, classify_confidence=str(cls.confidence),
            classify_reason=(cls.reason or "")[:1000])

        # 3) Extract
        ext = rfq_extractor_service.extract(
            email.get("subject", ""), email.get("body", ""),
            email.get("from_email", ""), email.get("from_name", ""))
        company = (ext.company or "")[:255] or None
        oscar_client.patch_rfq_request(
            rfq_id, company=company, extracted_json=_extraction_json(ext),
            status="extracted")

        # 4) Match
        lines = rfq_matcher_service.match_products(ext.products)
        matched = [l for l in lines if l.get("matched")]
        unmatched = [l for l in lines if not l.get("matched")]

        if not matched:
            oscar_client.patch_rfq_request(rfq_id, matched_json=lines, status="needs_review")
            task_id = None
            if create_task:
                task = _create_task(ext, rfq_id, lines, quotation=None,
                                    actor_user_id=actor_user_id)
                _post_first_comment(task, quotation=None, lines=lines)
                task_id = task["id"]
                oscar_client.patch_rfq_request(rfq_id, oscar_task_id=task_id)
            if mark_processed:
                _safe_mark(gmid)
            return {"status": "needs_review", "rfq_id": rfq_id, "task_id": task_id,
                    "company": ext.company, "matched": 0, "unmatched": len(unmatched),
                    "total": len(lines),
                    "unmatched_items": [u["requested"] for u in unmatched]}

        # 5) Quotation
        payload, resp = quotation_client.create_quotation(ext, lines)
        oscar_client.patch_rfq_request(
            rfq_id, matched_json=lines,
            quotation_id=resp.get("quotationId"),
            quotation_number=resp.get("quotationNumber"),
            editable_url=resp.get("editableUrl"), status="quoted")

        # Stamp the auto-generated quotation as "Oscar AI" — best-effort.
        try:
            qid = resp.get("quotationId")
            if qid:
                quotation_client.update_assignee(qid, "Oscar AI")
        except Exception as _e:
            logger.warning("[RFQ] assignee stamp 'Oscar AI' failed for %s: %s",
                           resp.get("quotationNumber"), _e)

        # 6) Oscar task + comment #1 (skipped on the chat/agent path)
        task_id = None
        if create_task:
            task = _create_task(ext, rfq_id, lines, quotation=resp,
                                actor_user_id=actor_user_id)
            task_id = task["id"]
            oscar_client.patch_rfq_request(rfq_id, oscar_task_id=task_id)
            _post_first_comment(task, quotation=resp, lines=lines)

        if mark_processed:
            _safe_mark(gmid)
        return {"status": "quoted", "rfq_id": rfq_id, "task_id": task_id,
                "company": ext.company, "customer": ext.customer_name,
                "quotation_number": resp.get("quotationNumber"),
                "editable_url": resp.get("editableUrl"),
                "subtotal": payload["summary"]["subTotal"],
                "matched": len(matched), "unmatched": len(unmatched), "total": len(lines),
                "unmatched_items": [u["requested"] for u in unmatched]}

    except Exception as e:
        logger.error("[RFQ] process_email failed for %s: %s", gmid, e, exc_info=True)
        try:
            current = oscar_client.get_rfq_request(gmid)
            attempt = (current.get("attempt_count") or 0) + 1 if current else 1
            oscar_client.patch_rfq_request(rfq_id, status="error", attempt_count=attempt)
        except OscarApiError as patch_err:
            logger.error("[RFQ] failed to record error status for %s: %s", gmid, patch_err)
        return {"status": "error", "rfq_id": rfq_id, "error": str(e)}


# ── Existing-quotation summary (repeat ask) ─────────────────────────────────
def _existing_summary(existing: dict) -> dict:
    """Build a chat summary from an already-processed rfq-request row so a repeat
    ask shows the stored quotation instead of 'no new RFQ'."""
    lines = existing.get("matched_json") or []
    matched = [l for l in lines if l.get("matched")]
    unmatched = [l for l in lines if not l.get("matched")]
    subtotal = round(sum(float(l.get("rate") or 0) * float(l.get("quantity") or 0)
                         for l in matched), 2)
    ext = existing.get("extracted_json") or {}
    return {
        "status": "duplicate",
        "rfq_id": existing["id"],
        "message_id": existing["gmail_message_id"],
        "already_processed": True,
        "company": existing.get("company") or ext.get("company"),
        "customer": ext.get("customer_name"),
        "quotation_number": existing.get("quotation_number"),
        "editable_url": existing.get("editable_url"),
        "subtotal": subtotal,
        "matched": len(matched), "unmatched": len(unmatched), "total": len(lines),
        "unmatched_items": [u.get("requested") for u in unmatched],
        "task_id": existing.get("oscar_task_id"),
    }


# ── Task + comment builders ─────────────────────────────────────────────────
def _create_task(ext: RfqExtraction, rfq_id: int, lines: list, quotation,
                 actor_user_id=None) -> dict:
    lead = resolve_team_lead(actor_user_id)
    if not lead:
        raise ValueError("no Team Lead resolved (set RFQ_TEAM_LEAD_USER_ID / RFQ_ORG_TEAM_ID)")

    matched = [l for l in lines if l.get("matched")]
    unmatched = [l for l in lines if not l.get("matched")]
    company = ext.company or ext.customer_name or "Unknown"
    qnum = quotation.get("quotationNumber") if quotation else "—"

    # Deadline for the team lead to act on the RFQ (IST-naive, per Oscar's convention).
    due_min = os.getenv("RFQ_TASK_DUE_MINUTES")
    if due_min:
        due_at = _now() + timedelta(minutes=int(due_min))
    else:
        due_at = _now() + timedelta(hours=int(os.getenv("RFQ_TASK_DUE_HOURS", "24")))

    desc = (
        f"Customer: {company}\n"
        f"Quotation: {qnum}\n"
        f"Respond By: {due_at.strftime('%d %b %H:%M')} IST\n"
        f"Priced {len(matched)} of {len(lines)} items ({len(unmatched)} not found — see chat)"
    )

    task = oscar_client.create_task(
        creator_user_id=lead["user_id"], title=f"RFQ - {company}"[:255],
        description=desc, assigned_to_user_id=lead["user_id"],
        priority="critical", status="pending", due_at=due_at.isoformat(),
    )
    # One clean push to the lead that a new RFQ task exists.
    try:
        oscar_client.send_notification(
            lead["user_id"], "task_assigned",
            f"New RFQ from {company} — quotation {qnum} ready to review",
            item_id=task["id"])
    except Exception as e:
        logger.warning("[RFQ] lead notify failed: %s", e)
    return task


def _post_first_comment(task: dict, quotation, lines):
    if quotation:
        body = (f"Quotation Generated\n\n"
                f"Quotation Number: {quotation.get('quotationNumber')}\n"
                f"Editable Link: {quotation.get('editableUrl')}")
    else:
        matched = sum(1 for l in lines if l.get("matched"))
        body = (f"RFQ received — {matched} of {len(lines)} products auto-matched.\n"
                f"No quotation was auto-generated; please prepare it manually.")
    oscar_client.post_task_comment(
        task["id"], author_user_id=task["assigned_to_user_id"], body=body,
        role="assistant", notify=False)


# ── helpers ─────────────────────────────────────────────────────────────────
def _extraction_json(ext: RfqExtraction) -> dict:
    return {
        "customer_name": ext.customer_name, "company": ext.company,
        "email": ext.email, "subject": ext.subject, "description": ext.description,
        "extraction_confidence": ext.extraction_confidence,
        "products": [p.model_dump() for p in ext.products],
    }


def _safe_mark(gmid: str):
    try:
        import gmail_service
        gmail_service.mark_processed(gmid, mark_read=True)
    except Exception as e:
        logger.warning("[RFQ] mark_processed failed for %s: %s", gmid, e)
