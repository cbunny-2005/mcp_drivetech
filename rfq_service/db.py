"""
Postgres connection — SINGLE shared connection for the catalog vector store
(pgvector). Lazy, thread-safe singleton, same pattern as gmail_service's
_build_service(): nothing connects until the first query needs it.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_conn = None
_conn_lock = threading.Lock()


def get_conn():
    """Return a live psycopg connection, reconnecting if the cached one died
    (e.g. after a long idle period on a free-tier DB host)."""
    global _conn
    with _conn_lock:
        if _conn is not None and not _conn.closed:
            return _conn

        import psycopg
        from pgvector.psycopg import register_vector

        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise EnvironmentError(
                "DATABASE_URL not set — see .env.example for the local Postgres "
                "connection string format."
            )
        _conn = psycopg.connect(url, autocommit=True)
        register_vector(_conn)
        logger.info("[DB] connected")
        return _conn
