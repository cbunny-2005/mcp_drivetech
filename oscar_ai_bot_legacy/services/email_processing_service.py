"""
Email processing service — THE ORCHESTRATOR for the RFQ pipeline.

Coordinates every step for one inbound email and is the only place that wires the
single-responsibility services together (gmail, classifier, extractor, matcher,
quotation_client, item_service, comment_service). Those services never call each
other directly — they go through here.

Flow (see docs/architecture/rfq-automation-plan.md):
  dedup -> INSERT pa_rfq_requests -> classify -> extract -> match ->
  quotation_client.create_quotation -> create Oscar task (owner=Team Lead) ->
  notify lead + comment #1 -> update row -> (optionally) mark email processed.
"""

import json
import logging
import os

from models.orm_models import RfqRequest, Team, User
from schemas.pydantic_schemas import RfqExtraction, RfqProduct
from services import (item_service, comment_service, quotation_client,
                      rfq_classifier_service, rfq_extractor_service,
                      rfq_matcher_service, notification_service)

logger = logging.getLogger(__name__)


def _now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def resolve_team_lead(db, actor_user_id=None):
    """Team Lead who owns the RFQ task. Routes to the ASKER's own team lead when
    we know who asked (so an Alumnx member's RFQ → Alumnx lead, a Turing member's
    → Turing lead). Falls back to RFQ_TEAM_LEAD_USER_ID / RFQ_ORG_TEAM_ID."""
    from models.orm_models import UserTeam
    if actor_user_id:
        memb = db.query(UserTeam).filter(
            UserTeam.user_id == actor_user_id, UserTeam.is_active == 1).first()
        if memb:
            lead = db.query(UserTeam).filter(
                UserTeam.team_id == memb.team_id,
                UserTeam.role == "team_lead", UserTeam.is_active == 1).first()
            if lead:
                u = db.query(User).filter(User.id == lead.user_id).first()
                if u:
                    return u
    # Fallback: explicit env lead, else the configured org team's owner.
    uid = os.getenv("RFQ_TEAM_LEAD_USER_ID")
    if uid:
        u = db.query(User).filter(User.id == int(uid)).first()
        if u:
            return u
    team_id = os.getenv("RFQ_ORG_TEAM_ID")
    if team_id:
        t = db.query(Team).filter(Team.id == int(team_id)).first()
        if t:
            return db.query(User).filter(User.id == t.owner_id).first()
    return None


def process_email(db, email: dict, mark_processed: bool = True,
                  create_task: bool = True, actor_user_id=None) -> dict:
    """Process one email end-to-end. `email` is a gmail_service.get_message() dict.
    Returns a summary dict. Idempotent on gmail_message_id.

    create_task=False → save the RFQ + generate the quotation but DON'T create an
    Oscar task/comment (used by the chat/agent path, which replies in chat instead)."""
    gmid = email["id"]

    # 1) Dedup — already seen. Instead of a bare "duplicate", return the STORED
    # quotation so the bot can show it again on a repeat ask (no re-quoting, no
    # duplicate task).
    existing = db.query(RfqRequest).filter(RfqRequest.gmail_message_id == gmid).first()
    if existing:
        return _existing_summary(existing)

    rfq = RfqRequest(
        gmail_message_id=gmid, gmail_thread_id=email.get("thread_id"),
        from_email=email.get("from_email"), from_name=email.get("from_name"),
        subject=(email.get("subject") or "")[:512],
        has_attachments=1 if email.get("has_attachments") else 0,
        status="received", received_at=_now(),
    )
    db.add(rfq); db.commit(); db.refresh(rfq)

    try:
        # 2) Classify
        cls = rfq_classifier_service.classify(
            email.get("subject", ""), email.get("body", ""), email.get("from_email", ""))
        rfq.is_rfq = 1 if cls.is_rfq else 0
        rfq.classify_confidence = str(cls.confidence)
        rfq.classify_reason = (cls.reason or "")[:1000]
        if not cls.is_rfq:
            rfq.status = "ignored"
            db.commit()
            if mark_processed:
                _safe_mark(gmid)
            return {"status": "ignored", "rfq_id": rfq.id, "reason": cls.reason}

        # 3) Extract
        ext = rfq_extractor_service.extract(
            email.get("subject", ""), email.get("body", ""),
            email.get("from_email", ""), email.get("from_name", ""))
        rfq.company = (ext.company or "")[:255] or None
        rfq.extracted_json = _extraction_json(ext)
        rfq.status = "extracted"
        db.commit()

        # 4) Match (agent uses the price-list tool)
        lines = rfq_matcher_service.match_products(ext.products)
        rfq.matched_json = lines
        matched = [l for l in lines if l.get("matched")]

        unmatched = [l for l in lines if not l.get("matched")]
        if not matched:
            rfq.status = "needs_review"
            db.commit()
            task_id = None
            if create_task:
                task = _create_task(db, ext, rfq, lines, quotation=None,
                                    actor_user_id=actor_user_id)
                _post_first_comment(db, task, quotation=None, lines=lines)
                task_id = task.id
            if mark_processed:
                _safe_mark(gmid)
            return {"status": "needs_review", "rfq_id": rfq.id, "task_id": task_id,
                    "company": ext.company, "matched": 0, "unmatched": len(unmatched),
                    "total": len(lines),
                    "unmatched_items": [u["requested"] for u in unmatched]}

        # 5) Quotation
        payload, resp = quotation_client.create_quotation(ext, lines)
        rfq.quotation_id = resp.get("quotationId")
        rfq.quotation_number = resp.get("quotationNumber")
        rfq.editable_url = resp.get("editableUrl")
        rfq.status = "quoted"
        db.commit()

        # Stamp the auto-generated quotation as "Oscar AI" (the quotations backend
        # defaults new quotations to "SuperAdmin"; this marks poller/agent-created
        # ones as machine-generated until a human reassigns the task, which then
        # re-patches the assignee to that person via item_service.update_item).
        # Best-effort: a patch failure must never break the pipeline.
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
            task = _create_task(db, ext, rfq, lines, quotation=resp,
                                actor_user_id=actor_user_id)
            rfq.oscar_task_id = task.id
            db.commit()
            _post_first_comment(db, task, quotation=resp, lines=lines)
            task_id = task.id

        if mark_processed:
            _safe_mark(gmid)
        return {"status": "quoted", "rfq_id": rfq.id, "task_id": task_id,
                "company": ext.company, "customer": ext.customer_name,
                "quotation_number": resp.get("quotationNumber"),
                "editable_url": resp.get("editableUrl"),
                "subtotal": payload["summary"]["subTotal"],
                "matched": len(matched), "unmatched": len(unmatched), "total": len(lines),
                "unmatched_items": [u["requested"] for u in unmatched]}

    except Exception as e:
        logger.error("[RFQ] process_email failed for %s: %s", gmid, e, exc_info=True)
        db.rollback()
        r = db.query(RfqRequest).filter(RfqRequest.id == rfq.id).first()
        if r:
            r.status = "error"
            r.attempt_count = (r.attempt_count or 0) + 1
            db.commit()
        return {"status": "error", "rfq_id": rfq.id, "error": str(e)}


# ── Existing-quotation summary (repeat ask) ─────────────────────────────────
def _existing_summary(existing: RfqRequest) -> dict:
    """Build a chat summary from an already-processed RfqRequest row so a repeat
    ask shows the stored quotation instead of 'no new RFQ'."""
    lines = existing.matched_json or []
    matched = [l for l in lines if l.get("matched")]
    unmatched = [l for l in lines if not l.get("matched")]
    subtotal = round(sum(float(l.get("rate") or 0) * float(l.get("quantity") or 0)
                         for l in matched), 2)
    ext = existing.extracted_json or {}
    return {
        "status": "duplicate",
        "rfq_id": existing.id,
        "message_id": existing.gmail_message_id,
        "already_processed": True,
        "company": existing.company or ext.get("company"),
        "customer": ext.get("customer_name"),
        "quotation_number": existing.quotation_number,
        "editable_url": existing.editable_url,
        "subtotal": subtotal,
        "matched": len(matched), "unmatched": len(unmatched), "total": len(lines),
        "unmatched_items": [u.get("requested") for u in unmatched],
        "task_id": existing.oscar_task_id,
    }


# ── Task + comment builders ─────────────────────────────────────────────────
def _bot_creator_id(db, lead) -> int:
    """The super-admin ("Oscar AI") that OWNS the created task. Falls back to the
    team lead if OSCAR_AI_BOT_ID is unset or the bot isn't a valid team member."""
    raw = os.getenv("OSCAR_AI_BOT_ID")
    if not raw:
        return lead.id
    try:
        bot_id = int(raw)
    except ValueError:
        return lead.id
    bot = db.query(User).filter(User.id == bot_id).first()
    return bot.id if bot else lead.id


def _create_task(db, ext: RfqExtraction, rfq: RfqRequest, lines: list, quotation,
                 actor_user_id=None):
    lead = resolve_team_lead(db, actor_user_id)
    if not lead:
        raise ValueError("no Team Lead resolved (set RFQ_TEAM_LEAD_USER_ID / RFQ_ORG_TEAM_ID)")
    # The TEAM LEAD owns the task. Oscar AI is a server-side entity (no team
    # membership), so it cannot be the create_item creator — the lead owns and is
    # assigned it (shows in their tasks). Oscar AI's role is captured in the
    # comment/quotation, not task ownership.
    creator_id = lead.id

    matched = [l for l in lines if l.get("matched")]
    unmatched = [l for l in lines if not l.get("matched")]
    company = ext.company or ext.customer_name or "Unknown"
    qnum = quotation.get("quotationNumber") if quotation else "—"
    status_line = "Quotation Generated" if quotation else "Needs manual quotation (no auto-match)"

    # Deadline for the team lead to act on the RFQ (IST-naive, per convention).
    # RFQ_TASK_DUE_MINUTES wins if set (e.g. 10 → due in 10 min, shows in Today);
    # else RFQ_TASK_DUE_HOURS (default 24h).
    from datetime import timedelta
    due_min = os.getenv("RFQ_TASK_DUE_MINUTES")
    if due_min:
        due_at = _now() + timedelta(minutes=int(due_min))
    else:
        due_at = _now() + timedelta(hours=int(os.getenv("RFQ_TASK_DUE_HOURS", "24")))

    # Minimal task: essentials only. The full details (matched/not-found lists)
    # live in the Oscar AI chat reply, not the task.
    desc = (
        f"Customer: {company}\n"
        f"Quotation: {qnum}\n"
        f"Respond By: {due_at.strftime('%d %b %H:%M')} IST\n"
        f"Priced {len(matched)} of {len(lines)} items ({len(unmatched)} not found — see chat)"
    )

    task = item_service.create_item(
        db, user_id=creator_id, item_type="task",   # super admin (Oscar AI) creates
        title=f"RFQ - {company}"[:255],
        description=desc, assigned_to_user_id=lead.id,   # assigned to the team lead (Alan)
        priority="critical", status="pending", due_at=due_at,   # deadline for the lead
    )
    # One clean push to the lead that a new RFQ task exists.
    try:
        notification_service.send(
            db, lead.id, "task_assigned",
            f"New RFQ from {company} — quotation {qnum} ready to review",
            item_id=task.id)
    except Exception as e:
        logger.warning("[RFQ] lead notify failed: %s", e)
    return task


def _post_first_comment(db, task, quotation, lines):
    if quotation:
        body = (f"Quotation Generated\n\n"
                f"Quotation Number: {quotation.get('quotationNumber')}\n"
                f"Editable Link: {quotation.get('editableUrl')}")
    else:
        matched = sum(1 for l in lines if l.get("matched"))
        body = (f"RFQ received — {matched} of {len(lines)} products auto-matched.\n"
                f"No quotation was auto-generated; please prepare it manually.")
    comment_service.post_comment(db, task, task.user_id, body,
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
        from services import gmail_service
        gmail_service.mark_processed(gmid, mark_read=True)
    except Exception as e:
        logger.warning("[RFQ] mark_processed failed for %s: %s", gmid, e)
