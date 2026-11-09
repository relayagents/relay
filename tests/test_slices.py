"""Slice-level checks: standups, digest, replay, meeting upload."""

from __future__ import annotations

from pathlib import Path

import httpx

from relayagents.api.app import build_services
from relayagents.core import projections
from relayagents.core.store import EventStore
from relayagents.tools.rest import TOOLS_PREFIX
from relayagents.workers.digest import post_digest
from tests.conftest import FIXTURES, auth, create_item


async def test_meeting_upload_with_transcript(client: httpx.AsyncClient, team, services) -> None:  # type: ignore[no-untyped-def]
    files = {
        "transcript": (
            "transcript_sample.json",
            (FIXTURES / "transcript_sample.json").read_bytes(),
            "application/json",
        )
    }
    r = await client.post(
        "/v1/meetings",
        data={"title": "Retrieval sync", "participants": "ada,grace,linus"},
        files=files,
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 202, r.text
    m = r.json()
    assert m["status"] == "queued" and m["participants"] == ["ada", "grace", "linus"]
    evs = (
        await client.get(
            "/v1/events", params={"thread": m["id"]}, headers=auth(team["ada"]["human"])
        )
    ).json()
    assert [e["type"] for e in evs] == ["meeting.started"]


async def test_meeting_upload_audio(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    files = {
        "audio": ("sample_audio.wav", (FIXTURES / "sample_audio.wav").read_bytes(), "audio/wav")
    }
    r = await client.post(
        "/v1/meetings", data={"title": "Audio"}, files=files, headers=auth(team["ada"]["human"])
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"
    bad = await client.post(
        "/v1/meetings",
        data={"title": "x"},
        files={"audio": ("evil.exe", b"MZ", "application/octet-stream")},
        headers=auth(team["ada"]["human"]),
    )
    assert bad.status_code == 400


async def test_standup_draft_cites_events_and_asks_instead_of_asserting(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    agent, human = team["grace"]["agent"], team["grace"]["human"]
    await client.post(
        "/v1/events",
        json={
            "payload": {
                "type": "action_item.created",
                "item_id": "item_9",
                "title": "Write the eval doc",
                "assignee": "grace",
            },
            "source": "api",
        },
        headers=auth(team["ada"]["human"]),
    )
    await create_item(client, team["ada"]["human"], "item_1", "grace", "Migrate the cache")
    await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "cache migration merged", "item_id": "item_1"},
        headers=auth(agent),
    )
    draft = (await client.get("/v1/standups/draft", headers=auth(agent))).json()
    assert draft["doing"] and "[evt_" in draft["doing"][0]
    assert draft["questions"] == [
        "Still working on 'Write the eval doc' (item_9)? No report in the last 24h."
    ]
    assert draft["cited_event_ids"]

    # mode=draft (default): DM with buttons, nothing posted, approval.requested emitted
    r = await client.post("/v1/standups", json=draft, headers=auth(agent))
    assert r.json()["mode"] == "draft" and r.json()["posted"] is False
    assert services.chat.dms[-1]["blocks"][1]["elements"][0]["action_id"] == "standup_approve"
    assert not any(p.get("channel") == "C_TEAM" for p in services.chat.posts)

    # mode=auto: posts with attribution and emits standup.posted
    await client.patch("/v1/me", json={"standup_mode": "auto"}, headers=auth(human))
    r = await client.post("/v1/standups", json=draft, headers=auth(agent))
    assert r.json()["posted"] is True
    assert services.chat.posts[-1]["attribution"] == "posted by Grace's agent"
    evs = (
        await client.get("/v1/events", params={"type": ["standup.posted"]}, headers=auth(human))
    ).json()
    assert (
        evs[0]["payload"]["mode"] == "auto"
        and evs[0]["provenance"]["parent_event_ids"] == draft["cited_event_ids"]
    )

    # someone else cannot submit grace's standup
    assert (
        await client.post("/v1/standups", json=draft, headers=auth(team["ada"]["agent"]))
    ).status_code == 403


async def test_digest_quiet_and_busy(services, team, client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    ev = await post_digest(services)
    assert ev.payload.quiet is True  # type: ignore[attr-defined]
    assert services.chat.posts[-1]["text"].endswith("no update.")
    await create_item(client, team["ada"]["human"], "item_1", "grace", "Ship the cache")
    await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "shipped the cache", "item_id": "item_1", "close_item": True},
        headers=auth(team["grace"]["agent"]),
    )
    ev = await post_digest(services)
    assert ev.payload.quiet is False and ev.payload.shipped == ["shipped the cache (grace)"]  # type: ignore[attr-defined]
    assert ev.provenance.parent_event_ids


async def test_replay_rebuilds_projections(services, team, client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    await client.post(
        "/v1/events",
        json={
            "payload": {
                "type": "action_item.created",
                "item_id": "item_r",
                "title": "Replay me",
                "assignee": "ada",
            }
        },
        headers=auth(team["ada"]["human"]),
    )
    async with services.db.session() as session:
        from sqlalchemy import delete

        from relayagents.core.models import ActionItemRow

        await session.execute(delete(ActionItemRow))
        await session.commit()
        assert await projections.list_items(session, assignee="ada") == []
        n = await projections.rebuild(session, EventStore(session))
        await session.commit()
        assert n >= 1
        assert [i.id for i in await projections.list_items(session, assignee="ada")] == ["item_r"]


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["db"] is True
    # *_configured fields are construction checks, not liveness probes: they must be
    # named accordingly so callers don't mistake them for reachability (issue #12).
    for field in ("slack_configured", "workspace_mcp_configured", "semantic_recall_configured"):
        assert field in body and isinstance(body[field], bool)
    for legacy in ("slack", "workspace_mcp", "semantic_recall"):
        assert legacy not in body


def test_workspace_mcp_is_off_unless_configured(settings) -> None:  # type: ignore[no-untyped-def]
    # A default URL used to build the connector with nothing behind it, so /health always said
    # workspace_mcp_configured: true.
    assert settings.workspace_mcp_url == ""
    assert build_services(settings).office is None
    on = settings.model_copy(update={"workspace_mcp_url": "http://workspace-mcp:8000/mcp"})
    assert build_services(on).office is not None
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    assert (
        "RELAY_WORKSPACE_MCP_URL: ${GOOGLE_OAUTH_CLIENT_ID:+http://workspace-mcp:8000/mcp}"
        in compose
    )


async def test_standup_lists_a_report_that_closed_its_item_only_as_done(
    client: httpx.AsyncClient, team
) -> None:  # type: ignore[no-untyped-def]
    # First compose boot: `report --close-item` put the same line under Done and under Doing.
    await create_item(client, team["ada"]["human"], "item_c", "grace", "Ship the cache")
    await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "cache half done", "item_id": "item_c"},
        headers=auth(team["grace"]["agent"]),
    )
    await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "cache shipped", "item_id": "item_c", "close_item": True},
        headers=auth(team["grace"]["agent"]),
    )
    await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "started on the eval doc"},
        headers=auth(team["grace"]["agent"]),
    )
    draft = (await client.get("/v1/standups/draft", headers=auth(team["grace"]["agent"]))).json()
    assert [d.split(" [")[0] for d in draft["done"]] == ["cache shipped"]
    # Only the report the closure came from is folded into Done; earlier progress stays.
    assert [d.split(" [")[0] for d in draft["doing"]] == [
        "cache half done",
        "started on the eval doc",
    ]
    assert draft["user_id"] == "grace"


def test_digest_says_each_thing_once() -> None:
    # First compose boot: `report --close-item` listed one line under Shipped and In progress, and
    # a posted standup repeats the events it cites, in a form _dedupe cannot match.
    from datetime import UTC, datetime, timedelta

    from relayagents.core.events import (
        ActionItemClosed,
        Actor,
        Event,
        Provenance,
        ReportPosted,
        StandupPosted,
    )
    from relayagents.workers.digest import build_digest

    agent = Actor.agent("grace.claude-code")
    report = Event.new(
        ReportPosted(text="cache shipped", item_id="item_c"), actor=agent, source="api"
    )
    closed = Event.new(
        ActionItemClosed(item_id="item_c", note="cache shipped"),
        actor=agent,
        source="api",
        provenance=Provenance(parent_event_ids=[report.id]),
    )
    other = Event.new(ReportPosted(text="eval doc started"), actor=agent, source="api")
    standup = Event.new(
        StandupPosted(
            user_id="grace",
            mode="auto",
            done=[f"cache shipped [{closed.id}]"],
            doing=[f"eval doc started [{other.id}]", "reviewing #12 [evt_from_last_week]"],
            cited_event_ids=[closed.id, other.id, "evt_from_last_week"],
        ),
        actor=agent,
        source="api",
    )
    now = datetime.now(UTC)
    d = build_digest(
        [report, closed, other, standup], window_start=now - timedelta(days=1), window_end=now
    )
    assert d.shipped == ["cache shipped (grace)"]
    assert d.in_progress == [
        "eval doc started (grace)",
        "reviewing #12 [evt_from_last_week] (grace)",
    ]


def test_digest_dedupe_never_hides_what_it_did_not_show() -> None:
    # Security review of the dedupe above: a teammate's closure must not hide someone's report, and
    # a standup line may be skipped only when its own cited event already has a line here.
    from datetime import UTC, datetime, timedelta

    from relayagents.core.events import (
        ActionItemClosed,
        Actor,
        DecisionMade,
        Event,
        Provenance,
        ReportPosted,
        StandupPosted,
    )
    from relayagents.workers.digest import build_digest

    grace, ada = Actor.agent("grace.claude-code"), Actor.human("ada")
    report = Event.new(
        ReportPosted(text="half the cache", item_id="item_c"), actor=grace, source="api"
    )
    closed_by_ada = Event.new(
        ActionItemClosed(item_id="item_c", note="ada closed it"),
        actor=ada,
        source="api",
        provenance=Provenance(parent_event_ids=[report.id]),
    )
    decision = Event.new(
        DecisionMade(decision_id="dec_1", statement="use pgvector"), actor=ada, source="api"
    )
    standup = Event.new(
        StandupPosted(
            user_id="grace",
            mode="auto",
            doing=[f"talked pgvector [{decision.id}]", f"claims ada's line [{closed_by_ada.id}]"],
            cited_event_ids=[decision.id],
        ),
        actor=grace,
        source="api",
    )
    now = datetime.now(UTC)
    d = build_digest(
        [report, closed_by_ada, decision, standup],
        window_start=now - timedelta(days=1),
        window_end=now,
    )
    assert d.shipped == ["ada closed it (ada)"]
    assert d.in_progress == [
        "half the cache (grace)",
        f"talked pgvector [{decision.id}] (grace)",
        f"claims ada's line [{closed_by_ada.id}] (grace)",
    ]
    assert {report.id, closed_by_ada.id, decision.id, standup.id} <= set(d.cited_event_ids)
