"""
RFQ worker — standalone entrypoint for the Gmail-polling RFQ pipeline.

This is a SEPARATE Render service from server.py (the MCP tool server) in this
same repo — same git repo, different deploy/process, per the "same repo, second
process" decision. It runs a background loop that watches the mailbox and, on
finding a NEW quotation-request (RFQ) email, does the needful automatically:
classify -> extract -> match prices -> generate the quotation -> CREATE A TASK
on Oscar (via oscar_client, the HTTP door into Oscar's /internal/* API) for the
team lead, with a deadline.

Dedup is by gmail_message_id (via Oscar's /internal/rfq-requests), so an email
is only ever turned into one task no matter how many times it's scanned. Emails
are left UNREAD (mark_processed=False) so a future on-demand check can still
surface them.

Config (env):
    RFQ_POLLER_INTERVAL_SEC     seconds between mailbox checks (default 120)
    RFQ_POLLER_MAX_RESULTS      unread emails scanned per tick (default 10)
    OSCAR_API_URL               Oscar backend base URL
    OSCAR_INTERNAL_SECRET       must match Oscar's INTERNAL_SERVICE_SECRET
    GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN
    RFQ_TEAM_LEAD_USER_ID / RFQ_ORG_TEAM_ID  (team-lead resolution fallback)
    RFQ_TASK_DUE_MINUTES / RFQ_TASK_DUE_HOURS

Run:
    python rfq_worker.py
"""

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import email_processing_service
import gmail_service
import price_lookup_service

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("rfq-worker")

_QUERY = "request for quotation OR quotation OR rfq OR enquiry"
_IST = ZoneInfo("Asia/Kolkata")


def _now():
    return datetime.now(_IST).replace(tzinfo=None)


def _interval() -> int:
    try:
        return max(30, int(os.getenv("RFQ_POLLER_INTERVAL_SEC", "120")))
    except ValueError:
        return 120


def _max_results() -> int:
    try:
        return max(1, int(os.getenv("RFQ_POLLER_MAX_RESULTS", "10")))
    except ValueError:
        return 10


def _tick():
    """One mailbox sweep. Crash-isolated by the caller."""
    if not gmail_service.is_configured():
        logger.warning("[RFQ-Worker] Gmail NOT configured (missing GMAIL_CLIENT_ID/"
                       "SECRET/REFRESH_TOKEN) — skipping tick")
        return

    ids = gmail_service.list_unread(query=_QUERY, max_results=_max_results())
    logger.info("[RFQ-Worker] tick — Gmail OK, query matched %d email(s)", len(ids or []))

    new_tasks = 0
    for mid in (ids or []):
        try:
            email = gmail_service.get_message(mid)
            r = email_processing_service.process_email(
                email, mark_processed=False, create_task=True)
            logger.info(
                "[RFQ-Worker]   msg=%s status=%s company=%s subject=%r task=%s",
                mid, r.get("status"), r.get("company"),
                (email.get("subject") or "")[:60], r.get("task_id"))
            if r.get("status") in ("quoted", "needs_review") and r.get("task_id"):
                new_tasks += 1
                logger.info(
                    "[RFQ-Worker] NEW RFQ processed: company=%s quote=%s task=%s",
                    r.get("company"), r.get("quotation_number"), r.get("task_id"))
        except Exception as e:
            logger.error("[RFQ-Worker] failed on message %s: %s", mid, e, exc_info=True)

    logger.info("[RFQ-Worker] tick done — %d new RFQ task(s) created (scanned %d)",
               new_tasks, len(ids or []))


def main():
    # Load price lists once up front so the first real RFQ isn't slow.
    try:
        price_lookup_service.load_price_lists()
    except Exception as e:
        logger.error("[RFQ-Worker] price-list preload failed: %s", e)

    interval = _interval()
    logger.info("[RFQ-Worker] started — checking the mailbox every %ds", interval)
    tick = 0
    while True:
        tick += 1
        try:
            _tick()
        except Exception as e:  # one bad tick must never kill the loop
            logger.error("[RFQ-Worker] tick %d crashed: %s", tick, e, exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
