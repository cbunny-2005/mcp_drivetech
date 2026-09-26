-- Catalog vector-search schema. Run once against a fresh database:
--   psql "$DATABASE_URL" -f schema.sql
--
-- catalog_items is fully rebuilt by embed_price_lists.py on every ingestion run
-- (TRUNCATE + re-insert) — no migrations needed for catalog data itself, only
-- for this schema.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS catalog_items (
    id                BIGSERIAL PRIMARY KEY,
    source_file       TEXT,
    sheet             TEXT,
    part_number       TEXT,
    norm_part         TEXT,
    description       TEXT,
    norm_description  TEXT,
    size_token        TEXT,
    price             NUMERIC,
    lead_time         TEXT,
    hsn               TEXT,
    embedding         VECTOR(1536),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stage 1 / Stage 2 exact-match lookups.
CREATE INDEX IF NOT EXISTS catalog_items_norm_part_idx ON catalog_items (norm_part);
CREATE INDEX IF NOT EXISTS catalog_items_norm_description_idx ON catalog_items (norm_description);

-- Stage 3 vector search. ivfflat needs ANALYZE after bulk load to pick good
-- cluster centroids — embed_price_lists.py runs ANALYZE after each ingestion.
CREATE INDEX IF NOT EXISTS catalog_items_embedding_idx
    ON catalog_items USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
