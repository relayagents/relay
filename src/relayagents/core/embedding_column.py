"""The width of ``events.embedding`` follows ``RELAY_EMBEDDING_DIM`` (ADR-0012).

Alembic owns the column; this module owns its width, because the width is configuration (it
depends on which embedding model the team runs), not history. ``relay migrate`` calls
:func:`sync_embedding_dim` after the Alembic upgrade, and relay-api's container runs
``relay migrate`` on every start, so changing the setting and restarting is the whole procedure.

Vectors are derived data (ADR-0001), so a width change drops them; the ``embed_backlog`` sweep
re-embeds the log with the new model, or ``relay replay --rebuild-graph`` does it at once.

Postgres only. SQLite stores vectors as JSON and has no width to manage.
"""

from __future__ import annotations

import structlog
from sqlalchemy import Connection, text

from relayagents.core.db import Database

log = structlog.get_logger()

HNSW_INDEX = "ix_events_embedding_hnsw"
# pgvector's HNSW index covers `vector` columns up to 2000 dimensions; wider needs `halfvec`.
MAX_INDEXED_DIM = 2000


def column_dim(conn: Connection) -> int | None:
    """The declared width of ``events.embedding``, or ``None`` when it has none (unsized
    ``vector``) or the backend is not Postgres. Raises if the column does not exist yet."""
    if conn.dialect.name != "postgresql":
        return None
    typmod = conn.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = to_regclass('events') AND attname = 'embedding' "
            "AND NOT attisdropped"
        )
    ).scalar_one_or_none()
    if typmod is None:
        raise RuntimeError("events.embedding does not exist; apply the Alembic migrations first")
    return typmod if typmod > 0 else None  # pgvector stores the width as the typmod; -1 = unsized


def sync_embedding_dim(conn: Connection, dim: int) -> bool:
    """Make ``events.embedding`` a ``vector(dim)`` with its HNSW index. Returns whether it changed.

    A change drops every stored vector: they came from a model of a different width and cannot be
    converted. Runs in the caller's transaction, so the drop, the type change and the new index
    land together or not at all. The index is built non-concurrently because the column is empty
    at that point, which makes the build instant.
    """
    if not 1 <= dim <= MAX_INDEXED_DIM:
        raise ValueError(f"embedding width must be 1..{MAX_INDEXED_DIM}, got {dim}")
    if conn.dialect.name != "postgresql":
        return False
    if column_dim(conn) == dim:
        return False  # the common case, on every relay-api start: no lock taken
    # Two relay-api replicas starting together must not both resize: lock, then look again.
    conn.execute(text("LOCK TABLE events IN ACCESS EXCLUSIVE MODE"))
    current = column_dim(conn)
    if current == dim:
        return False
    stored = conn.execute(text("SELECT count(*) FROM events WHERE embedding IS NOT NULL")).scalar()
    conn.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
    # USING NULL: old vectors cannot be cast to a new width, and they are derived data anyway.
    conn.execute(text(f"ALTER TABLE events ALTER COLUMN embedding TYPE vector({dim}) USING NULL"))
    conn.execute(
        text(f"CREATE INDEX {HNSW_INDEX} ON events USING hnsw (embedding vector_cosine_ops)")
    )
    log.warning(
        "embeddings.column_resized",
        old_dim=current,
        new_dim=dim,
        vectors_dropped=stored,
        next_step="the embed_backlog sweep re-embeds; `relay replay --rebuild-graph` does it now",
    )
    return True


async def ensure_embedding_dim(db: Database, dim: int) -> bool:
    """:func:`sync_embedding_dim` in its own transaction."""
    async with db.engine.begin() as conn:
        return await conn.run_sync(sync_embedding_dim, dim)


async def embedding_dim_mismatch(db: Database, dim: int) -> str | None:
    """Why vectors of width ``dim`` cannot be stored right now, or ``None`` when they can."""
    async with db.engine.connect() as conn:
        current = await conn.run_sync(column_dim)
        if conn.dialect.name != "postgresql" or current == dim:
            return None
    have = f"vector({current})" if current is not None else "an unsized vector"
    return (
        f"events.embedding is {have} but RELAY_EMBEDDING_DIM is {dim}; "
        "run `relay migrate` (relay-api runs it on start), then restart the workers"
    )
