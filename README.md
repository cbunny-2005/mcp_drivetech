# Quotations MCP Server

Standalone [MCP](https://modelcontextprotocol.io) server exposing the
**quotations-app backend** as tools. It's a thin **HTTP client** over the
quotations REST API — no database of its own — so it stays decoupled and can point
at local dev or the live Render backend.

```
MCP agent ──MCP──► server.py ──HTTP──► quotations-app backend ──► MongoDB
```

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
