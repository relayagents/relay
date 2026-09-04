"""`recall` merges the event-log, vector, and graph legs and always carries provenance."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from relayagents.connectors.memory.embeddings import embed_events, make_embedder
from relayagents.core.config import Settings
from relayagents.core.events import Event
from relayagents.core.protocols import MemoryHit
from relayagents.core.store import EventStore
from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth


class FakeMemory:
    def __init__(self) -> None:
        self.indexed: list[Event] = []

    async def index(self, events: Sequence[Event]) -> None:
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
        self.indexed.clear()

    async def close(self) -> None:
        return None


class FakeEmbedder:
    dim = 3

    async def __call__(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_make_embedder_needs_a_team_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert (
        make_embedder(Settings(embedding_model="openai:text-embedding-3-small", _env_file=None))
        is None
    )  # type: ignore[call-arg]
    assert make_embedder(Settings(embedding_model="local:nomic", _env_file=None)) is None  # type: ignore[call-arg]


async def test_recall_merges_legs_with_provenance(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    services.memory = FakeMemory()
    services.embedder = FakeEmbedder()
    tok = team["ada"]["human"]
    await client.post(
        "/v1/events",
        json={
            "payload": {
                "type": "decision.made",
                "decision_id": "dec_c",
                "statement": "We cache embeddings in pgvector keyed by content hash",
                "topic": "eval cache",
            },
            "source": "meeting",
        },
        headers=auth(tok),
    )
    r = await client.post(
        f"{TOOLS_PREFIX}/recall", json={"query": "pgvector embeddings cache"}, headers=auth(tok)
    )
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    kinds = {h["kind"] for h in hits}
    assert kinds == {"event", "graph"}  # vector leg is a no-op on SQLite
    for h in hits:
        assert h["event_ids"] or h["ref"]
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    only_events = (
        await client.post(
            f"{TOOLS_PREFIX}/recall",
            json={"query": "pgvector", "kinds": ["event"]},
            headers=auth(tok),
        )
    ).json()["hits"]
    assert {h["kind"] for h in only_events} == {"event"}


async def test_embed_events_writes_vectors(services, team) -> None:  # type: ignore[no-untyped-def]
    from relayagents.core.events import Actor, DecisionMade, ReportPosted

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
            Event.new(ReportPosted(text=""), actor=Actor.human("ada"), source="api")
        )
        await session.commit()
    n = await embed_events(services.db, FakeEmbedder(), [a, b])  # type: ignore[arg-type]
    assert n == 1  # empty text is skipped
    async with services.db.session() as session:
        from relayagents.core.models import EventRow

        row = await session.get(EventRow, 1)
        assert row is not None and row.embedding == [1.0, 0.0, 0.0]
