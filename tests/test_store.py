from __future__ import annotations

from datetime import UTC, datetime, timedelta

from relayagents.core import projections
from relayagents.core.events import (
    ActionItemClosed,
    ActionItemCreated,
    Actor,
    DecisionMade,
    Event,
    ReportPosted,
)
from relayagents.core.store import EventStore, parse_since
from relayagents.tools.context import Services


def test_parse_since() -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("2026-09-01T00:00:00+00:00") == datetime(2026, 9, 1, tzinfo=UTC)
    assert parse_since(None) is None


async def test_append_query_and_projections(services: Services) -> None:
    async with services.db.session() as session:
        store = EventStore(session)
        e1 = Event.new(
            ActionItemCreated(item_id="item_1", title="Add cache", assignee="grace"),
            actor=Actor.system("x"),
            source="meeting",
            thread_id="mtg_1",
        )
        e2 = Event.new(
            DecisionMade(decision_id="dec_1", statement="Use pgvector", topic="storage"),
            actor=Actor.human("ada"),
            source="meeting",
            thread_id="mtg_1",
        )
        e3 = Event.new(
            ReportPosted(text="cache table landed", item_id="item_1"),
            actor=Actor.agent("grace.hermes"),
            source="api",
        )
        e4 = Event.new(
            ActionItemClosed(item_id="item_1"), actor=Actor.agent("grace.hermes"), source="api"
        )
        for e in (e1, e2, e3, e4):
            await store.append(e)
            await projections.apply(session, e)
        await session.commit()

        assert await store.count() == 4
        assert [e.id for e in await store.query(thread_id="mtg_1")] == [e1.id, e2.id]
        assert [e.id for e in await store.query(user_id="grace")] == [e3.id, e4.id]
        assert [e.id for e in await store.query(types=["decision.made"])] == [e2.id]
        hits = await store.keyword_search("pgvector")
        assert [h[0].id for h in hits] == [e2.id]

        items = await projections.list_items(session, assignee="grace", status="all")
        assert len(items) == 1 and items[0].status == "closed"
        assert (await projections.list_decisions(session, topic="storage"))[
            0
        ].statement == "Use pgvector"

        # rebuild is idempotent
        n = await projections.rebuild(session, store)
        await session.commit()
        assert n == 4
        assert (await projections.list_items(session, assignee="grace", status="all"))[
            0
        ].status == "closed"


async def test_decision_supersedes(services: Services) -> None:
    async with services.db.session() as session:
        store = EventStore(session)
        a = Event.new(
            DecisionMade(decision_id="dec_a", statement="Deadline Oct 3"),
            actor=Actor.human("ada"),
            source="meeting",
        )
        b = Event.new(
            DecisionMade(decision_id="dec_b", statement="Deadline Oct 10", supersedes="dec_a"),
            actor=Actor.human("ada"),
            source="meeting",
        )
        for e in (a, b):
            await store.append(e)
            await projections.apply(session, e)
        ds = {d.id: d for d in await projections.list_decisions(session)}
        assert ds["dec_a"].superseded_by == "dec_b"
        assert ds["dec_b"].supersedes == "dec_a"
