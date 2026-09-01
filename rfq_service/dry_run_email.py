"""
Dry-run the email pipeline ONLY — Gmail fetch -> classify -> extract -> match ->
build (not submit) a quotation payload. Skips oscar_client entirely (no
rfq-request row, no task, no notification) and calls quotation_client in mock
mode, so nothing is written to Oscar's DB, the Quotation App, or Gmail
(mark_processed is never called; the email is left exactly as-is).

Usage (run from inside rfq_service/, same as rfq_worker.py):
    python dry_run_email.py                     # scan real unread mail
    python dry_run_email.py --message-id <id>   # one specific Gmail message
    python dry_run_email.py --sample            # no Gmail needed at all

Only these env vars matter here: OPENAI_API_KEY (required), GMAIL_CLIENT_ID/
GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN (only for real Gmail modes), and
PRICE_LIST_DIR (defaults to the bundled PRICE_LIST_excel/). OSCAR_* and
QUOTATION_* are irrelevant to this script.
"""

import argparse
import json
import os

from dotenv import load_dotenv
load_dotenv()  # local dev: reads rfq_service/.env

import gmail_service
import price_lookup_service
import quotation_client
import rfq_classifier_service
import rfq_extractor_service
import rfq_matcher_service

_SAMPLE_EMAIL = {
    "id": "sample-local-0001",
    "thread_id": "sample-thread",
    "from_email": "buyer@example.com",
    "from_name": "Test Buyer",
    "subject": "RFQ - SS Pipes and Gaskets",
    "date": "",
    "body": (
        "Hi,\n\nPlease send your best price for:\n"
        "1. 20 Nos SS 316 Elbow 25mm\n"
        "2. 50 Nos Gasket GKT 40mm\n\n"
        "Regards,\nTest Buyer"
    ),
    "snippet": "",
    "has_attachments": False,
}


def _run_one(email: dict) -> None:
    print(f"\n=== {email['subject']!r} (id={email['id']}) ===")

    cls = rfq_classifier_service.classify(
        email.get("subject", ""), email.get("body", ""), email.get("from_email", ""))
    print(f"[classify] is_rfq={cls.is_rfq} confidence={cls.confidence} reason={cls.reason!r}")
    if not cls.is_rfq:
        return

    ext = rfq_extractor_service.extract(
        email.get("subject", ""), email.get("body", ""),
        email.get("from_email", ""), email.get("from_name", ""))
    print(f"[extract] company={ext.company!r} customer={ext.customer_name!r} "
          f"products={len(ext.products)}")
    for p in ext.products:
        print(f"    - {p.product!r} qty={p.quantity} unit={p.unit}")

    lines = rfq_matcher_service.match_products(ext.products)
    matched = [l for l in lines if l.get("matched")]
    unmatched = [l for l in lines if not l.get("matched")]
    print(f"[match] matched={len(matched)} unmatched={len(unmatched)}")
    for l in lines:
        tag = "OK " if l["matched"] else "MISS"
        print(f"    [{tag}] {l['requested']!r} -> {l.get('description')!r} "
              f"rate={l.get('rate')} (method={l['match_method']})")

    if not matched:
        print("[quotation] skipped — nothing matched")
        return

    # QUOTATION_MOCK forces the client into mock mode regardless of
    # QUOTATION_API_URL, so this never calls the real Quotation App.
    os.environ["QUOTATION_MOCK"] = "1"
    payload = quotation_client.build_payload(ext, lines)
    print(f"[quotation] (payload only, mock submit) subTotal={payload['summary']['subTotal']}")
    print(json.dumps(payload, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message-id", help="one specific Gmail message id")
    ap.add_argument("--sample", action="store_true",
                    help="use a built-in fake email, no Gmail access needed")
    ap.add_argument("--max-results", type=int, default=5)
    args = ap.parse_args()

    price_lookup_service.load_price_lists()

    if args.sample:
        _run_one(_SAMPLE_EMAIL)
        return

    if not gmail_service.is_configured():
        raise SystemExit(
            "Gmail not configured (GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN) — "
            "use --sample to test without Gmail access.")

    if args.message_id:
        _run_one(gmail_service.get_message(args.message_id))
        return

    ids = gmail_service.list_unread(
        query="request for quotation OR quotation OR rfq OR enquiry",
        max_results=args.max_results)
    print(f"Found {len(ids)} unread candidate email(s)")
    for mid in ids:
        _run_one(gmail_service.get_message(mid))


if __name__ == "__main__":
    main()
