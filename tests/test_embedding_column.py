"""`events.embedding` follows RELAY_EMBEDDING_DIM (ADR-0012).

The Postgres tests need a throwaway pgvector database and **drop its public schema**; they run
when RELAY_TEST_POSTGRES_URL is set (CI's postgres job sets it), e.g.

    docker run --rm -d -p 55432:5432 -e POSTGRES_USER=relay -e POSTGRES_PASSWORD=relay \\
        -e POSTGRES_DB=relay pgvector/pgvector:pg16
    RELAY_TEST_POSTGRES_URL=postgresql+asyncpg://relay:relay@localhost:55432/relay \\
        uv run pytest tests/test_embedding_column.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from relayagents.cli import main as cli
from relayagents.connectors.memory.embeddings import disable_on_column_mismatch
from relayagents.core.config import Settings, reset_settings_cache
from relayagents.core.db import Database
from relayagents.core.embedding_column import (
    HNSW_INDEX,
    column_dim,
    embedding_dim_mismatch,
    ensure_embedding_dim,
)
from relayagents.core.events import Actor, DecisionMade, Event
from relayagents.core.store import EventStore
from relayagents.tools.context import Services

PG_URL = os.environ.get("RELAY_TEST_POSTGRES_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="RELAY_TEST_POSTGRES_URL not set")


class StubEmbedder:
    model_name = "stub"


async def test_sqlite_has_no_width_to_manage(services: Services) -> None:
    assert await ensure_embedding_dim(services.db, 768) is False
    assert await embedding_dim_mismatch(services.db, 768) is None
    services.embedder = StubEmbedder()  # type: ignore[assignment]
    await disable_on_column_mismatch(services)
    assert services.embedder is not None


@pytest.mark.parametrize("dim", [0, 2001])
async def test_width_outside_the_hnsw_limit_is_refused(services: Services, dim: int) -> None:
    with pytest.raises(ValueError, match=r"1\.\.2000"):
        await ensure_embedding_dim(services.db, dim)


# ---- Postgres ------------------------------------------------------------------------------


def _migrate(monkeypatch: pytest.MonkeyPatch, dim: int) -> str:
    monkeypatch.setenv("RELAY_DATABASE_URL", PG_URL)
    monkeypatch.setenv("RELAY_EMBEDDING_DIM", str(dim))
    reset_settings_cache()
    try:
        result = CliRunner().invoke(cli.app, ["migrate"])
    finally:
        reset_settings_cache()
    assert result.exit_code == 0, result.output
    return result.output


async def _reset_schema() -> None:
    db = Database(PG_URL)
    async with db.engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await db.dispose()


async def _state() -> dict[str, Any]:
    db = Database(PG_URL)
    async with db.engine.connect() as conn:
        dim = await conn.run_sync(column_dim)
        stored = (
            await conn.execute(text("SELECT count(*) FROM events WHERE embedding IS NOT NULL"))
        ).scalar()
        index = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": HNSW_INDEX}
            )
        ).scalar()
    await db.dispose()
    return {"dim": dim, "stored": stored, "index": index}


async def _store_one(dim: int) -> str:
    db = Database(PG_URL)
    event = Event.new(
        DecisionMade(decision_id="d1", statement="cache embeddings in pgvector"),
        actor=Actor.human("ada"),
        source="meeting",
    )
    async with db.session() as session:
        store = EventStore(session)
        await store.append(event)
        await store.set_embeddings([(event.id, [1.0] + [0.0] * (dim - 1))])
        await session.commit()
    await db.dispose()
    return event.id


async def _search(dim: int) -> list[str]:
    db = Database(PG_URL)
    async with db.session() as session:
        hits = await EventStore(session).vector_search([1.0] + [0.0] * (dim - 1), limit=5)
    await db.dispose()
    return [event.id for event, _ in hits]


@needs_pg
def test_relay_migrate_resizes_the_column_and_drops_stale_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_reset_schema())

    # Default width: what migration 0001 already created, so nothing changes or is printed.
    assert "resized" not in _migrate(monkeypatch, 1536)
    state = asyncio.run(_state())
    assert state["dim"] == 1536 and "hnsw" in state["index"]
    asyncio.run(_store_one(1536))

    # A local 768-d model: the column follows, old vectors go, the index is rebuilt.
    assert "resized to vector(768)" in _migrate(monkeypatch, 768)
    state = asyncio.run(_state())
    assert state == {"dim": 768, "stored": 0, "index": state["index"]}
    assert "hnsw" in state["index"] and "vector_cosine_ops" in state["index"]

    # 768-d vectors now store and search; running migrate again is a no-op.
    event_id = asyncio.run(_store_one(768))
    assert asyncio.run(_search(768)) == [event_id]
    assert "resized" not in _migrate(monkeypatch, 768)
    assert asyncio.run(_state())["stored"] == 1


@needs_pg
def test_workers_turn_the_vector_leg_off_on_a_mismatched_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_reset_schema())
    _migrate(monkeypatch, 1536)

    async def check(dim: int) -> tuple[str | None, object]:
        db = Database(PG_URL)
        settings = Settings(database_url=PG_URL, embedding_dim=dim, _env_file=None)  # type: ignore[call-arg]
        services = Services(db=db, settings=settings)
        services.embedder = StubEmbedder()  # type: ignore[assignment]
        reason = await embedding_dim_mismatch(db, dim)
        await disable_on_column_mismatch(services)
        await db.dispose()
        return reason, services.embedder

    reason, embedder = asyncio.run(check(1024))
    assert reason is not None and "vector(1536)" in reason and "relay migrate" in reason
    assert embedder is None

    reason, embedder = asyncio.run(check(1536))
    assert reason is None and embedder is not None
