# Drive Tech RFQ Worker

Standalone background worker that watches a Gmail mailbox for Request-For-Quotation
(RFQ) emails and automatically classifies, extracts, price-matches, and quotes
them — then creates a task on Oscar for the team lead to review.

This is a **separate Render service** from `../server.py` (the read-only
quotations MCP tool server) in this same repo — different deploy, different
process, no shared state. It was relocated out of the Oscar backend so
Drive-Tech-specific business logic (Gmail polling, OpenAI classify/extract/match,
the bundled price-list Excel files) doesn't live in Oscar's core.

```
Gmail mailbox ──poll──► rfq_worker.py ──OpenAI──► classify/extract/match
                              │
                              ├──HTTP──► Quotation App (quotations-app.onrender.com)
                              └──HTTP──► Oscar backend (/internal/* API)
```

## Why a separate process from server.py

`server.py` is a stateless, read-only MCP protocol server (stdio/HTTP transport,
no background work). This worker is a stateful background daemon that polls
Gmail on an interval, calls OpenAI, and writes to Oscar via HTTP. Bolting the two
together would mean one Render service running two unrelated jobs in the same
process — this keeps them independently deployable and independently
restartable.

## How it talks to Oscar

Oscar owns its own database; this worker has none. Every "write" this pipeline
needs to make inside Oscar (create a task, send a notification, post a comment,
resolve a team lead, record RFQ audit rows) goes through `oscar_client.py`, an
HTTP client authenticated with a single shared secret
(`X-Internal-Secret` header) against Oscar's `services/internal_api_router.py`
endpoints (`/internal/*`). See that file's docstring in the Oscar repo for the
full endpoint contract.

## Files

| File | Purpose |
|---|---|
| `rfq_worker.py` | Entrypoint — the poll loop |
| `email_processing_service.py` | Orchestrator: dedup → classify → extract → match → quote → create task |
| `rfq_classifier_service.py` | Is this email an RFQ? (OpenAI) |
| `rfq_extractor_service.py` | Extract customer + products from the email (OpenAI) |
| `rfq_matcher_service.py` | Match requested products to price-list rows (OpenAI + price_lookup_service) |
| `price_lookup_service.py` | Deterministic Excel price-list index and lookup — the ONLY source of prices |
| `quotation_client.py` | HTTP client to the external Quotation App |
| `oscar_client.py` | HTTP client to Oscar's `/internal/*` API |
| `gmail_service.py` | Single-account Gmail read/modify client |
| `rfq_schemas.py` | Pydantic models shared across the pipeline |
| `PRICE_LIST_excel/*.xlsx` | Bundled supplier price lists |

## Env

| Var | Required | Purpose |
|---|---|---|
| `OSCAR_API_URL` | ✅ | Oscar backend base URL, e.g. `https://developement-branch.onrender.com` |
| `OSCAR_INTERNAL_SECRET` | ✅ | Must match Oscar's `INTERNAL_SERVICE_SECRET` |
| `OPENAI_API_KEY` | ✅ | Classify/extract/match LLM calls |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | ✅ | Single-account Gmail auth (mint once via `gmail_oauth_bootstrap.py`) |
| `RFQ_TEAM_LEAD_USER_ID` | one of these | Fallback team lead if no `actor_user_id` context |
| `RFQ_ORG_TEAM_ID` | one of these | Fallback: that team's owner becomes the lead |
| `RFQ_POLLER_INTERVAL_SEC` | — | Default 120 |
| `RFQ_POLLER_MAX_RESULTS` | — | Unread emails scanned per tick, default 10 |
| `RFQ_TASK_DUE_MINUTES` / `RFQ_TASK_DUE_HOURS` | — | Task deadline; minutes wins if set, else hours (default 24) |
| `QUOTATION_API_URL` etc. | — | See `quotation_client.py` — same contract as before the move |
| `PRICE_LIST_DIR` | — | Default `PRICE_LIST_excel` (bundled) |

## Run

```bash
pip install -r requirements.txt
python rfq_worker.py
```

## Deploy (Render)

`render.yaml` in this folder declares a `worker`-type service
(`drivetech-rfq-worker`), separate from the root `mcp-drivetech` web service.
Set the env vars above in the Render dashboard (they're marked `sync: false` so
Render prompts for them rather than committing secrets).

**Migration note:** this worker must not run at the same time as Oscar's
in-process `rfq_poller.py` against the same mailbox/DB — that would double-process
every email (each side dedups independently). Cut over by disabling Oscar's
`DISABLE_RFQ_POLLER` only after this worker is confirmed processing correctly in
a dev/test pass.
