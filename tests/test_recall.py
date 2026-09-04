"""`recall`: keyword leg in the API, vector + graph legs in a worker; provenance everywhere."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
from pydantic_ai.embeddings import TestEmbeddingModel

from relayagents.connectors.memory.embeddings import PydanticAIEmbedder, embed_events, make_embedder
from relayagents.connectors.memory.search import semantic_search
from relayagents.core.config import Settings
from relayagents.core.events import Actor, DecisionMade, Event, ReportPosted
from relayagents.core.models import EMBEDDING_DIM, EventRow
from relayagents.core.protocols import MemoryHit
from relayagents.core.store import EventStore
from relayagents.tools.rest import TOOLS_PREFIX
from relayagents.workers.jobs import rebuild_graph, semantic_recall
from tests.conftest import auth


class FakeMemory:
    def __init__(self, fail: bool = False) -> None:
        self.indexed: list[Event] = []
        self.resets = 0
        self.fail = fail

    async def index(self, events: Sequence[Event]) -> None:
        if self.fail:
            raise RuntimeError("graph backend down")
        self.indexed.extend(events)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        return [
            MemoryHit(
                text="graph: the team caches embeddings in pgvector",
                score=0.9,
                kind="graph",
                event_ids=["evt_graph"],
                ref="edge_1",
            )
        ]

    async def reset(self) -> None:
        self.resets += 1
        self.indexed.clear()

    async def close(self) -> None:
        return None


class DimEmbedder:
    """A `core.protocols.Embedder` with the real column dimension."""

    model_name = "fake-1536"

    async def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (EMBEDDING_DIM - 1)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [await self.embed_query(t) for t in texts]


async def fake_semantic(
    query: str, *, limit: int = 10, kinds: Sequence[str] = ("vector", "graph")
) -> list[MemoryHit]:
    out = []
    if "vector" in kinds:
        out.append(
            MemoryHit(
                text="[decision.made] vector hit",
                score=0.7,
                kind="vector",
                event_ids=["evt_vec"],
                ref="evt_vec",
            )
        )
    if "graph" in kinds:
        out.append(
            MemoryHit(
                text="graph fact", score=0.9, kind="graph", event_ids=["evt_graph"], ref="edge_1"
            )
        )
    return out


def test_make_embedder_degrades_without_a_team_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert (
        make_embedder(Settings(embedding_model="openai:text-embedding-3-small", _env_file=None))
        is None
    )  # type: ignore[call-arg]
    assert make_embedder(Settings(embedding_model="nope:model", _env_file=None)) is None  # type: ignore[call-arg]
    assert make_embedder(Settings(embedding_model="", _env_file=None)) is None  # type: ignore[call-arg]


async def test_embedder_rejects_wrong_dimension() -> None:
    emb = PydanticAIEmbedder(TestEmbeddingModel(dimensions=8))
    with pytest.raises(ValueError, match="8 dimensions"):
        await emb.embed_query("x")


async def test_embedder_batches_and_truncates() -> None:
    emb = PydanticAIEmbedder(TestEmbeddingModel(dimensions=EMBEDDING_DIM))
    vecs = await emb.embed_documents(["a" * 20000] * 250)  # > one batch, > token limit per input
    assert len(vecs) == 250 and all(len(v) == EMBEDDING_DIM for v in vecs)


async def test_api_recall_merges_legs_with_provenance(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    services.semantic = fake_semantic
    tok = team["ada"]["human"]
    await client.post(
        "/v1/events",
        json={
            "payload": {
                "type": "decision.made",
                "decision_id": "dec_c",
                "statement": "We cache embeddings in pgvector keyed by content hash",
                "topic": "eval cache",
            }
        },
        headers=auth(tok),
    )
    hits = (
        await client.post(
            f"{TOOLS_PREFIX}/recall", json={"query": "pgvector embeddings cache"}, headers=auth(tok)
        )
    ).json()["hits"]
    assert {h["kind"] for h in hits} == {"event", "vector", "graph"}
    assert all(h["event_ids"] or h["ref"] for h in hits)
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    only = (
        await client.post(
            f"{TOOLS_PREFIX}/recall",
            json={"query": "pgvector", "kinds": ["graph"]},
            headers=auth(tok),
        )
    ).json()["hits"]
    assert {h["kind"] for h in only} == {"graph"}


async def test_api_recall_survives_a_broken_semantic_leg(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    async def boom(query: str, *, limit: int = 10, kinds: Sequence[str] = ()) -> list[MemoryHit]:
        raise RuntimeError("workers unreachable")

    services.semantic = boom
    r = await client.post(
        f"{TOOLS_PREFIX}/recall", json={"query": "anything"}, headers=auth(team["ada"]["human"])
    )
    assert r.status_code == 200 and all(h["kind"] == "event" for h in r.json()["hits"])


async def test_worker_semantic_search_runs_both_legs(services, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    services.embedder = DimEmbedder()
    services.memory = FakeMemory()
    async with services.db.session() as session:
        ev = await EventStore(session).append(
            Event.new(
                DecisionMade(decision_id="d1", statement="Use pgvector"),
                actor=Actor.human("ada"),
                source="meeting",
            )
        )
        await session.commit()

    async def fake_vector_search(self, embedding, *, limit=10):  # type: ignore[no-untyped-def]
        assert len(embedding) == EMBEDDING_DIM
        return [(ev, 0.8)]

    monkeypatch.setattr(EventStore, "vector_search", fake_vector_search)
    hits = await semantic_search(services, "pgvector", limit=5)
    assert [(h.kind, h.event_ids) for h in hits] == [("vector", [ev.id]), ("graph", ["evt_graph"])]
    # the arq job returns plain JSON
    raw = await semantic_recall({"services": services}, "pgvector", 5, ["vector"])
    assert raw[0]["kind"] == "vector" and raw[0]["event_ids"] == [ev.id]
    assert (
        await semantic_search(services, "   ", kinds=["vector"]) == []
    )  # blank query never hits the model


async def test_embed_events_writes_vectors_by_id(services) -> None:  # type: ignore[no-untyped-def]
    async with services.db.session() as session:
        store = EventStore(session)
        a = await store.append(
            Event.new(
                DecisionMade(decision_id="d", statement="Use pgvector"),
                actor=Actor.human("ada"),
                source="meeting",
            )
        )
        b = await store.append(
            Event.new(ReportPosted(text="   "), actor=Actor.human("ada"), source="api")
        )
        await session.commit()
    assert await embed_events(services.db, DimEmbedder(), [a, b]) == 1  # blank text is skipped
    async with services.db.session() as session:
        from sqlalchemy import select

        rows = {r.id: r.embedding for r in (await session.scalars(select(EventRow))).all()}
        assert rows[a.id] is not None and len(rows[a.id]) == EMBEDDING_DIM and rows[b.id] is None


async def test_rebuild_clears_and_reindexes_and_fails_loudly(services) -> None:  # type: ignore[no-untyped-def]
    services.embedder = DimEmbedder()
    services.memory = FakeMemory()
    async with services.db.session() as session:
        store = EventStore(session)
        ev = await store.append(
            Event.new(
                DecisionMade(decision_id="d", statement="Use pgvector"),
                actor=Actor.human("ada"),
                source="meeting",
            )
        )
        await store.set_embeddings([(ev.id, [9.0] * EMBEDDING_DIM)])
        await session.commit()
    assert await rebuild_graph({"services": services}) == 1
    assert services.memory.resets == 1 and [e.id for e in services.memory.indexed] == [ev.id]
    async with services.db.session() as session:
        row = await session.scalar(
            __import__("sqlalchemy").select(EventRow).where(EventRow.id == ev.id)
        )
        assert row is not None and row.embedding[0] == 1.0  # re-derived, not the stale vector
    services.memory = FakeMemory(fail=True)
    with pytest.raises(RuntimeError, match="graph backend down"):
        await rebuild_graph({"services": services})
