# Quotations MCP Server

Standalone [MCP](https://modelcontextprotocol.io) server exposing the
**quotations-app backend** as tools. It's a thin **HTTP client** over the
quotations REST API — no database of its own — so it stays decoupled and can point
at local dev or the live Render backend.

```
MCP agent ──MCP──► server.py ──HTTP──► quotations-app backend ──► MongoDB
```

## Other services in this repo

This is one of three independently-deployable pieces that happen to share a
git repo — no shared state, no shared process:

| Path | What it is |
|---|---|
| `server.py` (here) | This MCP server — read-only, queries the quotations backend |
| `rfq_service/` | Background worker: Gmail → classify/extract/match (pgvector catalog matching) → quotation → Oscar task. See `rfq_service/README.md` |
| `demo/` | Temporary FastAPI + Streamlit UI for demoing the `rfq_service` pipeline in a browser — not part of production, delete after use. See `demo/README.md` |
| `oscar_ai_bot_legacy/` | The old on-demand "Oscar AI" DM bot + its price lists, relocated OUT of the Oscar backend on 2026-09-26. Currently **not runnable as-is** — see below. |

## `oscar_ai_bot_legacy/` — the "Oscar AI" DM bot (moved 2026-09-26)

**What it was:** a second, focused RFQ path inside the Oscar backend (`AlumnxAILabs_epa`), separate from
the automatic background poller (`rfq_service/` above). A team member could DM a special "Oscar AI" account
inside the Oscar chat app and ask "any new RFQ emails?" — the bot would check the mailbox on demand and
reply in-chat with quotation details, instead of waiting for the background worker's own polling cycle.

**Why it's here now:** all RFQ-processing code was consolidated into this repo. This folder holds every
file that on-demand path depended on:

| File | Role |
|---|---|
| `oscar_ai_bot.py` | The focused LangChain agent — ONE tool (`check_rfq_emails`), a narrow system prompt, nothing else bound |
| `tools/rfq_tools.py` | The `check_rfq_emails` tool itself |
| `services/email_processing_service.py` | Pipeline: dedup → classify → extract → match → quote → create task |
| `services/rfq_classifier_service.py` | "Is this an RFQ?" (keyword prefilter + GPT-4o-mini) |
| `services/rfq_extractor_service.py` | Pulls products/customer/company out of the email |
| `services/rfq_matcher_service.py` | Matches extracted items against the price list (rapidfuzz + LLM pick) |
| `services/price_lookup_service.py` | Loads `PRICE_LIST_excel/*.xlsx` into an in-memory index at startup |
| `services/quotation_client.py` | Calls the Quotation App's `POST /api/quotations/generate` |
| `services/gmail_service.py` | Single-account Gmail OAuth client (shared RFQ inbox) |
| `scripts/create_oscar_bot.py` | One-time script: provisions the "Oscar AI" user account |
| `scripts/remove_oscar_bot_membership.py` | Removes that account from all teams (it's server-side, not a real team member) |
| `scripts/gmail_oauth_bootstrap.py` | Mints the Gmail refresh token these services need |
| `PRICE_LIST_excel/*.xlsx` | The actual company price lists `price_lookup_service.py` reads |

⚠️ **NOT currently runnable standalone.** `email_processing_service.py` imports Oscar-core services
(`item_service`, `comment_service`, `notification_service`) that only exist inside the Oscar backend's own
Python path — those aren't in this repo. To bring the DM bot back to life here, one of two things has to
happen first:

1. **Rewire it to call Oscar's `/internal/*` API** (the same pattern `rfq_service/oscar_client.py` in this
   repo already uses for the background worker) instead of importing `item_service`/`comment_service`
   directly — i.e. make `email_processing_service.py`'s task-creation step an HTTP call, not a local import.
2. Or run it co-located with a copy of the Oscar backend's `models`/`services` package on the Python path
   (not recommended — reintroduces the tight coupling this move was meant to remove).

**Automatic assignment behavior** (unchanged from when it lived in Oscar): every quotation this pipeline
creates is auto-assigned to **the team lead** of whichever team the asking user belongs to
(`resolve_team_lead` in `email_processing_service.py`) — not to the asker, and not to a fixed person. A task
carrying the quotation link is created and assigned the same way. If the asker has no team, it falls back to
`RFQ_TEAM_LEAD_USER_ID`, then the owner of `RFQ_ORG_TEAM_ID`.

**Config this path needs** (same names as `rfq_service/`'s own env vars — see that folder's `.env.example`):
`GMAIL_*` (OAuth creds for the shared inbox), `OPENAI_API_KEY`, `QUOTATION_API_URL` /
`QUOTATION_FRONTEND_URL`, `RFQ_TEAM_LEAD_USER_ID`, `RFQ_ORG_TEAM_ID`, `RFQ_TASK_DUE_MINUTES` /
`RFQ_TASK_DUE_HOURS`, `OSCAR_AI_BOT_ID` (the provisioned bot account's user id in Oscar's own database).

**As of this move, the "Oscar AI" DM feature is OFF inside the live Oscar app** — the code that triggered it
(`main.py`'s DM-interception hook) has been removed there. Reviving it means finishing option 1 or 2 above,
then re-adding an equivalent trigger wherever DMs are handled going forward.

## Tools
Purpose is **conversational intelligence** — let an agent answer natural-language
questions about quotations (counts, values, who-has-what, lookups) and take actions.

**Conversational / query (read):**
| Tool | Answers |
|---|---|
| `quotation_stats()` | "how many are pending?", "total pipeline value?", "who has the most?" — counts by status + value by assignee |
| `search_quotations(company?, status?, assignee?, number?, min_total?, max_total?)` | "show quotations assigned to Vijender over ₹10k" — filtered list + total value |
| `find_by_number(quotation_number)` | resolves a human number like `QT-2026-000005` to the full quotation |
| `list_quotations()` | all quotations (compact) |
| `get_quotation(quotation_id)` | one full quotation by internal id |
| `health()` | backend reachability + count |

**READ-ONLY by design.** This MCP only *queries* the customer's data and answers
questions — it **never writes** (no create/reassign). Creating or modifying
quotations is the customer app's job. The backend exposes only list + get, so
filtering/aggregation is done here; prices/totals come only from the
server-computed data — never invented.

### Completion / status
A quotation is "completed" only if the customer's data **stores** that state. Today
their backend hard-codes `status:"draft"` and never changes it, so `quotation_stats`
/ `search_quotations(status=...)` will only ever report `draft`. The moment their
app writes a real status (e.g. `completed`/`sent`), these read tools surface it
automatically — no change here.

## Install
```bash
pip install -r requirements.txt   # mcp[cli], httpx
```

## Run
```bash
# stdio (dev / desktop MCP clients)
python server.py

# HTTP endpoint (for the MCP agent to connect to)
MCP_TRANSPORT=http MCP_PORT=8200 python server.py
# → MCP endpoint at http://<host>:8200/mcp

# inspect/try tools
mcp dev server.py
```

## Env
| Var | Default | Purpose |
|---|---|---|
| `QUOTATIONS_API_URL` | `https://quotations-app.onrender.com` | backend base URL |
| `QUOTATIONS_TIMEOUT` | `30` | HTTP timeout (bump for Render cold start ~50s) |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8200` | bind for http transport |

## Notes
- Deployed **separately** from the quotations backend and from Oscar.
- The Oscar agent will later connect to this server (as an MCP client) via a new
  endpoint — this repo/folder does not depend on Oscar.
