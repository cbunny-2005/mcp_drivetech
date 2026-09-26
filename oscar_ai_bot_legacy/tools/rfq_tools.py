import logging

from langchain_core.tools import tool
from models.database import SessionLocal

logger = logging.getLogger(__name__)


def _rfq_entry(r: dict, email: dict, already_processed: bool) -> dict:
    """Shape one RFQ result for the chat/DM summary."""
    return {
        "company": r.get("company"),
        "customer": r.get("customer"),
        "subject": email.get("subject"),
        "quotation_number": r.get("quotation_number"),
        "editable_url": r.get("editable_url"),
        "subtotal": r.get("subtotal"),
        "matched": r.get("matched"),
        "unmatched": r.get("unmatched"),
        "total": r.get("total"),
        "unmatched_items": r.get("unmatched_items", []),
        "task_id": r.get("task_id"),
        "already_processed": already_processed,
        "status": r.get("status"),
    }


def make_rfq_tools(user_id: int) -> list:

    @tool
    def check_rfq_emails(max_results: int = 5) -> dict:
        """Check the RFQ mailbox for new quotation-request (RFQ) emails and prepare
        quotations for them. Reads unread emails, detects which are RFQs, extracts the
        requested products, matches them to the company price lists, and generates a
        draft quotation for each. Prices/part numbers come ONLY from the price lists —
        never invented.

        Use this when the user asks things like: "did we get any RFQ/quotation emails",
        "check for new quotation requests", "any new RFQs", "make quotations for new
        enquiry emails". Returns a summary the assistant relays in the chat.
        """
        from services import (gmail_service, price_lookup_service,
                              email_processing_service)
        if not gmail_service.is_configured():
            return {"error": "Gmail is not connected — cannot check for RFQ emails."}
        try:
            price_lookup_service.load_price_lists()
            ids = gmail_service.list_unread(
                query="request for quotation OR quotation OR rfq OR enquiry",
                max_results=max_results,
            )
        except Exception as e:
            logger.error("[RFQ-TOOL] fetch failed: %s", e)
            return {"error": f"Couldn't read the mailbox: {e}"}

        db = SessionLocal()
        try:
            rfqs, skipped = [], 0
            for mid in ids:
                email = gmail_service.get_message(mid)
                # Oscar AI (super admin) path: save + quote + CREATE A TASK assigned
                # to the team lead. Keep emails unread (mark_processed=False).
                r = email_processing_service.process_email(
                    db, email, mark_processed=False, create_task=True,
                    actor_user_id=user_id)   # route the task to the asker's team lead
                st = r.get("status")
                if st == "ignored":
                    skipped += 1
                elif st == "duplicate":
                    # Already processed: surface the STORED quotation on a repeat
                    # ask (only if it actually produced a quotation).
                    if r.get("quotation_number"):
                        rfqs.append(_rfq_entry(r, email, already_processed=True))
                elif st in ("quoted", "needs_review"):
                    rfqs.append(_rfq_entry(r, email, already_processed=False))
            return {
                "scanned": len(ids),
                "rfq_count": len(rfqs),
                "non_rfq_skipped": skipped,
                "rfqs": rfqs,
                "note": ("Prices are from the company price lists only. New RFQs are "
                         "assigned as a task to the team lead; repeat asks show the "
                         "already-created quotation."),
            }
        except Exception as e:
            db.rollback()
            logger.error("[RFQ-TOOL] processing failed: %s", e, exc_info=True)
            return {"error": f"Something went wrong processing the RFQ emails: {e}"}
        finally:
            db.close()

    return [check_rfq_emails]
