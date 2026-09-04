"""Slice-level checks: standups, digest, replay, meeting upload."""

from __future__ import annotations

import httpx

from relayagents.core import projections
from relayagents.core.store import EventStore
from relayagents.tools.rest import TOOLS_PREFIX
from relayagents.workers.digest import post_digest
from tests.conftest import FIXTURES, auth


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
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["db"] is True
