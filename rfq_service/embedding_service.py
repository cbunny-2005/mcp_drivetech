"""
Embedding service — the ONE place both the catalog ingestion script and the
live matcher turn text into vectors. Sharing this module is what keeps the two
sides in the same vector space: a catalog row and an RFQ request must go
through identical normalization before embedding, or their vectors won't be
comparable.

Also owns size-token extraction (kept OUT of the embedding text — see
price_lookup_service module docstring for why — and used as a separate
ranking signal instead).
"""

import logging
import os
import re

from openai import OpenAI

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT_SEC", "20")))
    return _client


# ── Normalization ───────────────────────────────────────────────────────────
# Domain jargon a general-purpose embedding model won't reliably know. Applied
# to BOTH catalog descriptions (at ingestion) and RFQ request text (at query
# time) so the two sides describe the same part in comparable words.
_ABBREVIATIONS = {
    r"\btc\b":     "tri-clamp",
    r"\bss\b":     "stainless steel",
    r"\bgkt\b":    "gasket",
    r"\bbspf\b":   "bsp female thread",
    r"\bbspm\b":   "bsp male thread",
    r"\bph\b":     "pipe hanger",
    r"\bm\.s\b":   "mild steel",
    r"\bms\b":     "mild steel",
    r"\bhsg\b":    "housing",
    r"\bqty\b":    "quantity",
    r"\bnos\.?\b": "numbers",
    r"\bch pl\b":  "channel plate",
    r"\bdn\b":     "diameter nominal",
}
_ABBR_PATTERNS = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in _ABBREVIATIONS.items()]


def normalize_for_embedding(text: str) -> str:
    """Lowercase, expand known abbreviations, collapse whitespace. Keeps
    spaces/structure (unlike price_lookup_service's strip-everything _norm)
    since embeddings work on natural-language-ish text, not exact tokens."""
    if not text:
        return ""
    s = text.lower()
    for pattern, repl in _ABBR_PATTERNS:
        s = pattern.sub(repl, s)
    s = re.sub(r"[_,]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── Size-token extraction ───────────────────────────────────────────────────
# Matches: 2", 1.5", 4in, 12.7 TO 65 MM, 50mm, DN25, 1-2in. Deliberately loose
# — this feeds a fuzzy size-match BOOST in ranking, not an exact filter.
_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?)?\s*(?:mm|in|inch|\"|')|dn\s*\d+)",
    re.IGNORECASE,
)


def extract_size_token(text: str) -> str | None:
    """Pull the first size-looking token out of free text, normalized to a
    bare comparable form (digits + unit, no spaces). None if nothing matches."""
    if not text:
        return None
    m = _SIZE_RE.search(text)
    if not m:
        return None
    token = m.group(1).lower()
    token = token.replace('"', "in").replace("'", "in")
    token = re.sub(r"\s+", "", token)
    return token or None


# ── Embedding calls ──────────────────────────────────────────────────────────
def embed(text: str) -> list[float]:
    """Embed a single string. Empty/whitespace-only text returns a zero
    vector (caller should avoid this — it embeds to nothing meaningful)."""
    text = (text or "").strip()
    if not text:
        return [0.0] * EMBEDDING_DIM
    resp = _get_client().embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def embed_batch(texts: list[str], batch_size: int = 100) -> list[list[float]]:
    """Embed many strings, batched to keep request payloads reasonable.
    Preserves input order. Empty strings embed to a zero vector without an
    API call (OpenAI rejects empty input)."""
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        # Empty strings must not go to the API — replace with a placeholder,
        # zero out afterward so a blank description never gets a bogus vector.
        safe_chunk = [t.strip() or "-" for t in chunk]
        resp = _get_client().embeddings.create(model=EMBEDDING_MODEL, input=safe_chunk)
        for orig, item in zip(chunk, resp.data):
            out.append(item.embedding if orig.strip() else [0.0] * EMBEDDING_DIM)
        logger.info("[EMBED] embedded %d/%d", min(start + batch_size, len(texts)), len(texts))
    return out
