"""Tiny shared text helper used by both embed_price_lists.py (ingestion) and
price_lookup_service.py (live queries) — kept separate from
embedding_service.normalize_for_embedding, which prepares text FOR embedding
(keeps structure/spaces) rather than for exact-string / keyword comparison
(strips everything to bare alphanumeric tokens)."""

import re


def norm_exact(s) -> str:
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()
