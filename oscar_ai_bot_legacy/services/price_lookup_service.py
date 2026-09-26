"""
Price lookup service — deterministic matching against the supplier Excel lists.

Loads EVERY .xlsx in PRICE_LIST_excel/ once at startup into an in-memory index
and answers lookups. This is the ONLY source of prices and part numbers — the LLM
never prices anything. Matching cascade: exact part number → normalized
description → fuzzy (rapidfuzz). Returns candidates + confidence; never invents.

The two shipped files have DIFFERENT schemas, so columns are mapped by HEADER
NAME (not fixed positions):
  File 1 (HFH, sheet Sheet2, header row 2):
    ALSIS codes | AL Item code | Description | LPL 2026 (INR) | Anytime ordering
  File 2 (HHT, sheet GPHE, header row 1):
    Item code | Anytime Code | Desc | List Price - July onward |
    DLP (40% Discount on LP) | HSN | Lead time (In weeks)
Rate = LIST price (the DLP/discount column is intentionally ignored — MVP sends
discount=0 and the Team Lead applies discounts while editing the quotation).
"""

import glob
import logging
import os
import re

logger = logging.getLogger(__name__)

PRICE_DIR = os.getenv("PRICE_LIST_DIR", "PRICE_LIST_excel")

# Header keyword → canonical field. First match wins; order matters.
_PART_KEYS = ("al item code", "item code", "anytime code", "part", "code")
_DESC_KEYS = ("description", "desc")
_PRICE_KEYS = ("lpl", "list price", "price", "rate")   # NOT "dlp"/"discount"
_LEAD_KEYS = ("lead time", "delivery", "lead")
_HSN_KEYS = ("hsn",)

_rows: list[dict] = []      # in-memory index of ProductRow dicts
_loaded = False


# ── Normalization ───────────────────────────────────────────────────────────
def _norm(s) -> str:
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _to_price(v):
    if v is None:
        return None
    try:
        return round(float(str(v).replace(",", "").strip()), 2)
    except (ValueError, TypeError):
        return None


def _match_header(cell: str, keys) -> bool:
    c = _norm(cell)
    # DLP/discount columns must never be treated as the list price.
    if keys is _PRICE_KEYS and ("dlp" in c or "discount" in c):
        return False
    return any(k in c for k in keys)


# ── Load ────────────────────────────────────────────────────────────────────
def load_price_lists(directory: str = None) -> int:
    """Load all .xlsx in `directory` into the in-memory index. Returns row count.
    Idempotent — safe to call again to reload (e.g. after dropping a new file)."""
    global _rows, _loaded
    directory = directory or PRICE_DIR
    import openpyxl

    new_rows: list[dict] = []
    files = sorted(glob.glob(os.path.join(directory, "*.xlsx")))
    if not files:
        logger.warning("[PRICE] no .xlsx found in %s", directory)

    for path in files:
        fname = os.path.basename(path)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        file_rows = 0
        for ws in wb.worksheets:
            colmap, header_row_idx = _find_header(ws)
            if not colmap or "description" not in colmap and "part_number" not in colmap:
                continue
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i <= header_row_idx:
                    continue
                rec = _row_to_record(row, colmap, fname, ws.title)
                if rec is None:
                    continue
                new_rows.append(rec)
                file_rows += 1
        wb.close()
        logger.info("[PRICE] loaded %d rows from %s", file_rows, fname)

    _rows = new_rows
    _loaded = True
    logger.info("[PRICE] index ready: %d rows from %d file(s)", len(_rows), len(files))
    return len(_rows)


def _best_column(cells, keys):
    """Pick the column whose header matches the MOST SPECIFIC keyword (earliest in
    `keys`, which is ordered most-specific-first). Prevents 'ALSIS codes' (matches
    generic 'code') from beating 'AL Item code' (matches 'al item code')."""
    best_idx, best_rank = None, len(keys)
    for idx, cell in enumerate(cells):
        c = _norm(cell)
        if not c:
            continue
        if keys is _PRICE_KEYS and ("dlp" in c or "discount" in c):
            continue
        for rank, k in enumerate(keys):
            if k in c and rank < best_rank:
                best_idx, best_rank = idx, rank
                break
    return best_idx


def _find_header(ws):
    """Scan the first ~8 rows for the header row; return (colmap, header_row_idx).
    colmap maps canonical field → column index (best/most-specific header per field)."""
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i > 8:
            break
        cells = [("" if c is None else str(c)) for c in row]
        if not any(cells):
            continue
        colmap = {}
        for field, keys in (
            ("part_number", _PART_KEYS), ("description", _DESC_KEYS),
            ("price", _PRICE_KEYS), ("lead_time", _LEAD_KEYS), ("hsn", _HSN_KEYS),
        ):
            idx = _best_column(cells, keys)
            if idx is not None:
                colmap[field] = idx
        # Two fields must not share a column (e.g. description vs part). Keep the
        # more specific assignment; drop the collision from the looser field.
        _dedupe_colmap(colmap, cells)
        # A valid header row must have a description or part number AND a price.
        if ("description" in colmap or "part_number" in colmap) and "price" in colmap:
            return colmap, i
    return {}, -1


def _dedupe_colmap(colmap, cells):
    seen = {}
    for field in list(colmap.keys()):
        idx = colmap[field]
        if idx in seen:
            # keep whichever field's header is longer/more specific; drop the other
            other = seen[idx]
            if len(_norm(cells[idx])) and field == "part_number":
                del colmap[other]
                seen[idx] = field
            else:
                del colmap[field]
        else:
            seen[idx] = field


def _row_to_record(row, colmap, fname, sheet):
    def cell(field):
        idx = colmap.get(field)
        return row[idx] if idx is not None and idx < len(row) else None

    part = cell("part_number")
    desc = cell("description")
    if (part is None or str(part).strip() == "") and (desc is None or str(desc).strip() == ""):
        return None  # trailing empty row (File 1 pads to ~1M rows)
    price = _to_price(cell("price"))
    return {
        "source_file": fname,
        "sheet": sheet,
        "part_number": str(part).strip() if part is not None else None,
        "description": str(desc).strip() if desc is not None else None,
        "price": price,
        "lead_time": (str(cell("lead_time")).strip() if cell("lead_time") is not None else None),
        "hsn": (str(cell("hsn")).strip() if cell("hsn") is not None else None),
        "_norm_desc": _norm(desc),
        "_norm_part": _norm(part),
    }


# ── Lookup ──────────────────────────────────────────────────────────────────
def lookup(query: str, part_number: str = None, top_k: int = 3,
           fuzzy_threshold: int = 70) -> dict:
    """Match a requested product against the price list. Deterministic.
    Returns {matched, method, confidence, best, candidates}."""
    if not _loaded:
        load_price_lists()

    # 1) Exact part number (highest confidence)
    if part_number:
        npart = _norm(part_number)
        for r in _rows:
            if r["_norm_part"] and r["_norm_part"] == npart:
                return _result(True, "exact_part", 1.0, r, [r])

    nq = _norm(query)
    if not nq:
        return _result(False, "empty_query", 0.0, None, [])

    # 2) Exact normalized description
    exact = [r for r in _rows if r["_norm_desc"] == nq]
    if exact:
        return _result(True, "exact_desc", 1.0, exact[0], exact[:top_k])

    # 3) Fuzzy over descriptions
    try:
        from rapidfuzz import process, fuzz
        choices = [(idx, r["_norm_desc"]) for idx, r in enumerate(_rows) if r["_norm_desc"]]
        # token_set_ratio handles subset queries well ("T8-M2 CH PL 316" vs the
        # full "T8-M2 CH PL 316/0.5/HT/NBRP…") where token_sort penalizes length.
        scored = process.extract(
            nq, {i: d for i, d in choices},
            scorer=fuzz.token_set_ratio, limit=top_k,
        )
        cands = [dict(_rows[i], _score=round(sc, 1)) for (_d, sc, i) in scored]
    except ImportError:
        cands = []

    if cands and cands[0].get("_score", 0) >= fuzzy_threshold:
        conf = round(cands[0]["_score"] / 100.0, 3)
        return _result(True, "fuzzy", conf, cands[0], cands)
    return _result(False, "no_match", 0.0, None, cands)


def _result(matched, method, confidence, best, candidates):
    def clean(r):
        if r is None:
            return None
        return {k: v for k, v in r.items() if not k.startswith("_norm")}
    return {
        "matched": matched,
        "method": method,
        "confidence": confidence,
        "best": clean(best),
        "candidates": [clean(c) for c in candidates],
    }


def index_size() -> int:
    return len(_rows)
