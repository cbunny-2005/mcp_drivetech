"""
Demo backend — TEMPORARY, for demoing the RFQ matching pipeline only.
No database: everything lives in memory for the process lifetime (see
pipeline.py). Delete this whole demo/ folder after the demo.

Run (from demo/backend/):
    uvicorn main:app --reload --port 8010
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import pipeline

app = FastAPI(title="RFQ Pipeline Demo (temporary)")

# Wide open on purpose — this is a localhost-only demo, never do this in
# anything that isn't thrown away after the demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/runs")
def create_run():
    """Scan unread RFQ emails right now and run classify -> extract -> match
    -> quotation (mock submit) for each. Returns the newly created runs."""
    try:
        return pipeline.run_dry_run()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/runs")
def get_runs():
    return pipeline.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    record = pipeline.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return record


class ItemPatch(BaseModel):
    itemName: str | None = None
    partNumber: str | None = None
    quantity: float | None = None
    unitPrice: float | None = None
    discountPercentage: float | None = None
    delivery: str | None = None


@app.patch("/api/runs/{run_id}/items/{line_number}")
def patch_item(run_id: str, line_number: int, body: ItemPatch):
    """Edit any field on one quotation line; the summary is always
    recomputed from the full item list afterward, never trusted from input."""
    record = pipeline.patch_item(run_id, line_number, body.model_dump(exclude_unset=True))
    if record is None:
        raise HTTPException(status_code=404, detail="Run or line item not found")
    return record
