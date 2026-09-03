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
| `price_lookup_service.py` | Deterministic catalog lookup against Postgres/pgvector — the ONLY source of prices |
| `embed_price_lists.py` | One-off ingestion script — run manually whenever the `.xlsx` files change |
| `embedding_service.py` | Shared text normalization + OpenAI embedding calls (catalog ingestion AND live queries go through this, so both sides land in the same vector space) |
| `text_utils.py` | Shared exact-match text normalizer |
| `db.py` | Lazy Postgres connection singleton |
| `schema.sql` | `catalog_items` table + pgvector extension + indexes — apply once per database |
| `quotation_client.py` | HTTP client to the external Quotation App |
| `oscar_client.py` | HTTP client to Oscar's `/internal/*` API |
| `gmail_service.py` | Single-account Gmail read/modify client |
| `rfq_schemas.py` | Pydantic models shared across the pipeline |
| `PRICE_LIST_excel/*.xlsx` | Bundled supplier price lists (source data — ingested into Postgres, not read at runtime) |

## Catalog matching (pgvector)

Product matching is a 3-stage cascade in `price_lookup_service.lookup()`:

1. **Exact part number** — free, deterministic.
2. **Exact normalized description** — same.
3. **Every remaining catalog row is scored directly and reranked** — not a
   truncated shortlist per signal, the full ~2,500 rows:
   ```
   score = 0.35 × cosine_similarity          (pgvector, semantic)
         + 0.5  × rapidfuzz.token_set_ratio  (literal word overlap)
         + 0.15 × size_boost × max(cosine, fuzzy)   (tiebreaker, scaled by relevance)
   ```
   Two lessons from testing against real RFQ data shaped this:
   - **Why fuzzy is weighted above vector, not below:** catalog descriptions
     range from two words ("SUPPORT SECTOR") to long technical sentences, and
     RFQ text is long and numeric-heavy. Vector-only retrieval buried a
     genuinely correct match 2456th out of 2554 rows by cosine similarity
     alone — a short description drifts far from a long query in embedding
     space even when it's the right part. `token_set_ratio` doesn't care
     about length, so on *this* catalog's short/literal part names it proved
     the more reliable signal; vector search still earns its keep on
     reworded/synonym requests fuzzy matching misses.
   - **Why the size boost is scaled by `max(cosine, fuzzy)` instead of added
     flat:** an unscaled size match let a wrong-type row (a gasket sharing
     the requested item's literal size number) outrank a right-type row that
     simply had no parsed size at all — measured directly as a ~₹1M swing in
     a real quote before this fix. Size is a tiebreaker among already-
     relevant rows, not an independent vote.
   - **Why the whole catalog is scored instead of a top-N pool per signal:**
     an earlier version truncated each signal to its own shortlist *before*
     blending, which dropped a row whose blended score would have ranked it
     highly but that individually missed both signals' own narrow cutoff. At
     ~2,500 rows, scoring everything is cheap enough to remove that failure
     mode outright rather than tune pool widths per fixture.

The reranked candidates still go to the same LLM agent in `rfq_matcher_service.py`
for the final type-safety judgment (e.g. rejecting a "ferrule housing" for a plain
"ferrule" request) — this module only ranks, it never decides or invents a price.

### Local Postgres + pgvector setup

**Managed Postgres (Render, Supabase, etc.):** pgvector is usually
preinstalled — skip straight to "One-time setup" below and just run
`CREATE EXTENSION vector;` in your database.

**Docker (any OS — the easiest local path, avoids compiling anything):**
the pgvector project publishes ready-made images with the extension already
built in.

```bash
docker run -d --name drivetech-pg -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 pgvector/pgvector:pg16
docker exec -it drivetech-pg psql -U postgres -c "CREATE DATABASE drivetech_rfq;"
docker exec -it drivetech-pg psql -U postgres -d drivetech_rfq -c "CREATE EXTENSION vector;"
```

Then `DATABASE_URL=postgres://postgres:postgres@localhost:5432/drivetech_rfq`.

**Native Postgres already installed — Homebrew, macOS:**
`brew install pgvector` installs the extension against Homebrew's own
Postgres build — nothing to compile yourself. Then jump to "Create the
database" below.

**Native Postgres already installed — EDB installer / Postgres.app-style,
macOS (no Homebrew):** the `vector` extension isn't bundled and has to be
compiled from source against *your* `pg_config`, because the extension
binary must match your exact Postgres build:

```bash
git clone --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
export PG_CONFIG=/Library/PostgreSQL/18/bin/pg_config   # adjust to your version/path
make
```

If `make` fails immediately with `fatal error: 'inttypes.h' file not found`
pointing at a path like `.../SDKs/MacOSX14.sdk`, your Postgres build expects
an SDK version your current Xcode Command Line Tools no longer ship (only
newer ones remain under `/Library/Developer/CommandLineTools/SDKs/`).
Symlink the missing name to whichever SDK you do have, then re-run `make`:

```bash
ls /Library/Developer/CommandLineTools/SDKs/               # see what you actually have
sudo ln -s /Library/Developer/CommandLineTools/SDKs/MacOSX15.sdk \
           /Library/Developer/CommandLineTools/SDKs/MacOSX14.sdk
```

Then install — pass `PG_CONFIG` directly on the `make` command line, not via
`export`, because `sudo` on macOS resets the environment by default and
silently drops exported variables:

```bash
sudo make install PG_CONFIG=/Library/PostgreSQL/18/bin/pg_config
```

**Native Postgres already installed — Windows:** pgvector's own build
targets MSVC, not the mingw/gcc toolchain, so you need Visual Studio's C++
build tools (the free "Build Tools for Visual Studio" installer with the
"Desktop development with C++" workload is enough — a full Visual Studio IDE
install isn't required). From an **"x64 Native Tools Command Prompt for VS"**
(Start menu, installed alongside the build tools — a plain `cmd.exe` won't
have the right compiler on PATH):

```bat
set "PGROOT=C:\Program Files\PostgreSQL\16"
git clone --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

Adjust `PGROOT` to your actual Postgres install directory and version. If
`nmake` isn't recognized, you're in a regular Command Prompt/PowerShell
window, not the VS-provided one — reopen via "x64 Native Tools Command
Prompt for VS" from the Start menu.

**Create the database and enable the extension** (same for every native
install method, once pgvector is built/installed):

```bash
psql -h localhost -U postgres -c "CREATE DATABASE drivetech_rfq;"
psql -h localhost -U postgres -d drivetech_rfq -c "CREATE EXTENSION vector;"
```

Point `DATABASE_URL` in `.env` at it, e.g.
`postgres://postgres:<password>@localhost:5432/drivetech_rfq` — see
`.env.example` for the full local-vs-Render format.

### One-time setup / whenever the price lists change

```bash
# 1. Apply the schema once per database (creates the catalog_items table + indexes)
psql "$DATABASE_URL" -f schema.sql

# 2. (Re-)embed the catalog — TRUNCATEs and reloads catalog_items from PRICE_LIST_DIR
python embed_price_lists.py
```

`embed_price_lists.py` is NOT run automatically by the worker — the worker only
ever reads `catalog_items`. Run it manually (or as a one-off Render job) after
uploading new `.xlsx` files, then restart the worker.

## Demo UI (temporary)

`../demo/` has a throwaway FastAPI + Streamlit UI that runs this same pipeline
on-demand and lets you inspect/edit the resulting quotation in a browser
instead of a terminal. It only imports and calls these modules — nothing here
depends on it, and it's meant to be deleted after use. See `../demo/README.md`.

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
| `PRICE_LIST_DIR` | — | Default `PRICE_LIST_excel` (bundled); read only by `embed_price_lists.py`, not at worker runtime |
| `DATABASE_URL` | ✅ | Postgres connection string for the pgvector catalog store — see `.env.example` for local vs. Render format |

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
