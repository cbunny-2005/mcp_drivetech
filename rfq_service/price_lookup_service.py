"""
Price lookup service — deterministic matching against the vector-embedded
catalog in Postgres/pgvector. This is the ONLY source of prices and part
numbers — the LLM never prices anything. The catalog itself is populated by
embed_price_lists.py, run manually whenever the .xlsx price lists change;
this module only ever reads.

Matching cascade:
  1. Exact part number       — free, deterministic, always right when it hits.
  2. Exact normalized description — same.
  3. Every remaining catalog row is scored directly (cosine similarity +
     fuzzy-text score + size-token match, blended) and reranked — reached
     only when 1-2 miss.

     Why score the whole catalog instead of truncating to a top-N pool per
     signal first: at ~2,500 rows, scoring every row is cheap, and an
     earlier truncate-then-union design measurably dropped correct matches.
     Two failure modes were caught by testing against real RFQ data:
     (a) vector-only retrieval ranked a genuinely correct match ("SUPPORT
     SECTOR" for a "GENERIC SUPPORT FOR PIPE..." request) 2456th of 2554 by
     cosine similarity alone — terse catalog descriptions drift far from
     long, numeric-heavy RFQ text in embedding space; (b) even after adding
     a fuzzy leg, truncating EACH leg to its own top-N before computing the
     blended score dropped a row ("CLAMPS AND SCREWS" for a "PH CLAMPS
     HEAVY" request) whose blended score would have ranked it highly, because
     it individually missed both legs' own narrow top-N cutoff — several
     unrelated catalog rows outranked it on cosine alone, and several
     literal-token-heavy rows outranked it on fuzzy alone, even though
     neither alone was the better match. Scoring the full catalog before
     ranking removes that failure mode structurally instead of tuning pool
     widths against one fixture at a time.

Returns candidates + confidence; never invents. rfq_matcher_service's LLM
agent still makes the final type-safety judgment over whatever candidates
this returns — this module's job is only to rank, not to decide.
"""

import logging
import threading

from pgvector import Vector
from psycopg.rows import dict_row
from rapidfuzz import process, fuzz

import db
import embedding_service
from text_utils import norm_exact

logger = logging.getLogger(__name__)

# Hybrid rerank weights — sum to 1.0. Fuzzy carries the most weight: measured
# against this catalog (short, literal technical part names — "SUPPORT
# SECTOR", "CLAMPS AND SCREWS", "CLAMP"), rapidfuzz.token_set_ratio was
# consistently the more reliable signal, while cosine similarity alone
# sometimes buried the correct match hundreds of ranks down (terse
# descriptions drift far from long, numeric-heavy RFQ text in embedding
# space). Vector search still earns its keep on reworded/synonym requests
# fuzzy matching misses on its own — it's a supporting signal here, not the
# lead one, for THIS catalog's text characteristics. Size is a smaller
# tiebreaker signal since not every request/row has one.
_W_VECTOR, _W_FUZZY, _W_SIZE = 0.35, 0.5, 0.15

# Reported "matched" only if the top hybrid score clears this floor. Below it,
# the pool is too weak to be worth an LLM call — same intent as the old
# fuzzy_threshold, just measured against the hybrid score instead of rapidfuzz.
_MATCH_FLOOR = 0.5

_CANDIDATE_FIELDS = ("source_file", "sheet", "part_number", "description",
                     "price", "lead_time", "hsn", "size_token")


def lookup(query: str, part_number: str = None, size_token: str = None,
           top_k: int = 8) -> dict:
    """Match a requested product against the catalog. Returns
    {matched, method, confidence, best, candidates}."""
    conn = db.get_conn()

    # 1) Exact part number (highest confidence)
    if part_number:
        npart = norm_exact(part_number)
        row = _fetch_one(
            conn, "SELECT * FROM catalog_items WHERE norm_part = %s LIMIT 1", (npart,))
        if row:
            c = _clean(row)
            return _result(True, "exact_part", 1.0, c, [c])

    nq = norm_exact(query)
    if not nq:
        return _result(False, "empty_query", 0.0, None, [])

    # 2) Exact normalized description
    exact_rows = _fetch_all(
        conn, "SELECT * FROM catalog_items WHERE norm_description = %s LIMIT %s",
        (nq, top_k))
    if exact_rows:
        cands = [_clean(r) for r in exact_rows]
        return _result(True, "exact_desc", 1.0, cands[0], cands)

    # 3) Score every catalog row: cosine similarity (vector) via SQL, fuzzy
    # text score via rapidfuzz over the in-process index — both computed for
    # the FULL catalog, not a truncated top-N, then blended and reranked.
    embed_text = embedding_service.normalize_for_embedding(query)
    qvec = Vector(embedding_service.embed(embed_text))
    vec_rows = _fetch_all(
        conn,
        "SELECT id, source_file, sheet, part_number, description, "
        "norm_description, size_token, price, lead_time, hsn, "
        "1 - (embedding <=> %s) AS cosine_sim FROM catalog_items",
        (qvec,))
    if not vec_rows:
        return _result(False, "no_match", 0.0, None, [])

    fuzzy_by_id = {}
    fuzzy_index = _load_fuzzy_index()
    if fuzzy_index:
        choices = {i: r["norm_description"] for i, r in enumerate(fuzzy_index)
                   if r["norm_description"]}
        hits = process.extract(nq, choices, scorer=fuzz.token_set_ratio, limit=None)
        for _desc, score, idx in hits:
            fuzzy_by_id[fuzzy_index[idx]["id"]] = score / 100.0

    q_size = size_token or embedding_service.extract_size_token(query)

    ranked = []
    for row in vec_rows:
        cosine = float(row["cosine_sim"])
        fuzzy = fuzzy_by_id.get(row["id"], 0.0)
        base_relevance = max(cosine, fuzzy)
        size_boost = _size_match_score(q_size, row.get("size_token"))
        # Size is a TIEBREAKER, not an independent signal — it's scaled by
        # base_relevance so a coincidental size match (e.g. a gasket that
        # happens to mention the same size as a requested clamp) can't
        # outrank a genuinely relevant item that simply lacks a parsed size.
        score = (_W_VECTOR * cosine + _W_FUZZY * fuzzy
                 + _W_SIZE * size_boost * base_relevance)
        ranked.append((score, row))
    ranked.sort(key=lambda x: x[0], reverse=True)

    candidates = []
    for score, row in ranked[:top_k]:
        c = _clean(row)
        c["_score"] = round(score * 100, 1)
        candidates.append(c)

    top_score = candidates[0]["_score"] / 100.0
    matched = top_score >= _MATCH_FLOOR
    return _result(matched, "vector" if matched else "no_match",
                   round(top_score, 3), candidates[0] if matched else None, candidates)


def _size_match_score(q_size, row_size):
    if not q_size:
        return 0.5   # no size info in the request — signal is neutral, not penalizing
    if not row_size:
        return 0.4   # catalog row has no size token — slightly below neutral
    return 1.0 if q_size == row_size else 0.0


_fuzzy_index_cache = None
_fuzzy_index_lock = threading.Lock()


def _load_fuzzy_index():
    """In-process cache of every catalog row's text fields, for the fuzzy
    retrieval leg. Loaded once per process — the catalog only changes via a
    fresh embed_price_lists.py run followed by a worker restart, same
    lifecycle assumption the old in-memory Excel cache made."""
    global _fuzzy_index_cache
    with _fuzzy_index_lock:
        if _fuzzy_index_cache is not None:
            return _fuzzy_index_cache
        conn = db.get_conn()
        _fuzzy_index_cache = _fetch_all(
            conn,
            "SELECT id, source_file, sheet, part_number, description, "
            "norm_description, size_token, price, lead_time, hsn FROM catalog_items",
            ())
        return _fuzzy_index_cache


def _clean(row: dict) -> dict:
    """Keep only the fields downstream code (rfq_matcher_service) uses — drop
    id/embedding/created_at/norm_*/cosine_sim, and coerce price to float."""
    out = {k: row.get(k) for k in _CANDIDATE_FIELDS}
    if out.get("price") is not None:
        out["price"] = round(float(out["price"]), 2)
    return out


def _fetch_one(conn, sql, params):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetch_all(conn, sql, params):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _result(matched, method, confidence, best, candidates):
    return {
        "matched": matched,
        "method": method,
        "confidence": confidence,
        "best": best,
        "candidates": candidates,
    }


def index_size() -> int:
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM catalog_items")
        return cur.fetchone()[0]
