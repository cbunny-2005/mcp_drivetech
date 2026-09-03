# RFQ Pipeline Demo (TEMPORARY)

Wraps the real `rfq_service` pipeline (classify → extract → match → quote,
mock-submit only) behind a FastAPI backend and a Streamlit frontend, so it
can be clicked through in a browser instead of read off a terminal. No
database — results live in memory for the backend process's lifetime.

**Delete this whole `demo/` folder after the demo.** It is not part of the
production pipeline; it only imports and calls the real `rfq_service`
modules, never modifies them.

## Run

Uses the same `mcp_drivetech/.venv` and `rfq_service/.env` as the rest of
the repo — no separate credentials needed.

```bash
# Terminal 1 — backend
cd mcp_drivetech
source .venv/bin/activate
uv pip install -r demo/backend/requirements.txt
cd demo/backend
uvicorn main:app --reload --port 8010

# Terminal 2 — frontend
cd mcp_drivetech
source .venv/bin/activate
uv pip install -r demo/frontend/requirements.txt
cd demo/frontend
streamlit run app.py
```

Open the Streamlit URL it prints (usually http://localhost:8501), click
**Run Dry Run** to scan unread RFQ emails and process them, then pick a run
from the dropdown to see classification, extraction, match results, and
the generated quotation — editable inline.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/runs` | Scan unread RFQ emails now, run the full pipeline on each, cache the results |
| `GET` | `/api/runs` | List cached runs |
| `GET` | `/api/runs/{run_id}` | Full detail for one run |
| `PATCH` | `/api/runs/{run_id}/items/{line_number}` | Edit any field on one quotation line; summary is always recomputed server-side |
