"""The standup draft's Approve button posts once, however often it is clicked (ADR-0008)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from relayagents.core.events import Actor
from relayagents.core.models import ApprovalRow, UserRow
from relayagents.core.store import EventStore
from relayagents.tools.context import Services
from relayagents.workers.standup import (
    StandupDraft,
    post_standup_from_draft,
    run_due_standups,
    submit,
)
from tests.conftest import auth


class FakeSlackClient:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []

    async def chat_update(self, **kw: Any) -> None:
        self.updates.append(kw)

    async def chat_postEphemeral(self, **kw: Any) -> None:  # noqa: N802 (Slack's name)
        self.ephemeral.append(kw)


async def _standups_in_channel(services: Services) -> list[dict[str, Any]]:
    return [p for p in services.chat.posts if p["channel"] == "C_TEAM" and not p.get("update")]  # type: ignore[union-attr]


async def test_a_second_approve_click_does_not_post_again(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    await submit(
        services,
        StandupDraft(user_id="grace", doing=["evals"]),
        actor=Actor.agent("grace.claude-code"),
    )
    approve = services.chat.dms[-1]["blocks"][1]["elements"][0]  # type: ignore[union-attr]
    body = {
        "actions": [{"value": approve["value"]}],
        "user": {"id": "UGRACE"},
        "channel": {"id": "DGRACE"},
        "message": {"ts": "1.000"},
    }
    client = FakeSlackClient()
    await post_standup_from_draft(services, body, client)
    await post_standup_from_draft(services, body, client)  # a double click, or Slack retrying
    assert len(await _standups_in_channel(services)) == 1
    async with services.db.session() as session:
        posted = await EventStore(session).query(types=["standup.posted"])
    assert len(posted) == 1
    assert "already posted" in client.updates[-1]["text"].lower()


async def _draft_for_grace(services: Services) -> tuple[str, dict[str, Any]]:
    out = await submit(
        services,
        StandupDraft(user_id="grace", doing=["evals"]),
        actor=Actor.agent("grace.claude-code"),
    )
    approve = services.chat.dms[-1]["blocks"][1]["elements"][0]  # type: ignore[union-attr]
    body = {
        "actions": [{"value": approve["value"]}],
        "user": {"id": "UGRACE"},
        "channel": {"id": "DGRACE"},
        "message": {"ts": "1.000"},
    }
    return out["approval_id"], body


async def _posted(services: Services) -> int:
    async with services.db.session() as session:
        return len(await EventStore(session).query(types=["standup.posted"]))


async def test_a_repeated_cli_approval_posts_once(client, services: Services, team) -> None:  # type: ignore[no-untyped-def]
    approval_id, _ = await _draft_for_grace(services)
    for _ in range(2):
        r = await client.post(
            f"/v1/approvals/{approval_id}/resolve",
            json={"decision": "approved"},
            headers=auth(team["grace"]["human"]),
        )
        assert r.status_code == 200, r.text
    assert await _posted(services) == 1


async def test_concurrent_clicks_post_once(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    _, body = await _draft_for_grace(services)
    client = FakeSlackClient()
    await asyncio.gather(*(post_standup_from_draft(services, body, client) for _ in range(3)))
    assert await _posted(services) == 1


async def test_an_expired_draft_is_not_posted(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    approval_id, body = await _draft_for_grace(services)
    async with services.db.session() as session:
        row = await session.get(ApprovalRow, approval_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    client = FakeSlackClient()
    await post_standup_from_draft(services, body, client)
    assert await _posted(services) == 0
    assert "expired" in client.updates[-1]["text"]
    async with services.db.session() as session:
        row = await session.get(ApprovalRow, approval_id)
        assert row is not None and row.status == "expired"


async def test_a_scheduled_standup_is_threaded_by_the_users_local_date(
    services: Services, team
) -> None:  # type: ignore[no-untyped-def]
    async with services.db.session() as session:
        user = await session.get(UserRow, "linus")
        assert user is not None
        user.timezone, user.standup_mode = "Pacific/Auckland", "auto"
        await session.commit()
    # 09:00 on 15 June in Auckland is 21:00 on 14 June in UTC
    assert await run_due_standups(services, now=datetime(2026, 6, 14, 21, 0, tzinfo=UTC)) == [
        "linus"
    ]
    async with services.db.session() as session:
        (ev,) = await EventStore(session).query(types=["standup.posted"])
    assert ev.thread_id == "standup:linus:2026-06-15"


async def test_a_draft_whose_approval_never_committed_does_not_post(
    services: Services, team, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # Slack accepted the draft DM, then the commit failed: the approval behind its buttons was
    # rolled back. The next tick sends a fresh draft. Only that one may post; the dead one used
    # to post as "the owner's click is the approval".
    chat = services.chat
    assert chat is not None
    real_dm = chat.dm
    calls = 0

    async def dm_then_fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        ref = await real_dm(*args, **kwargs)
        if args[0] == "grace":
            calls += 1
        if args[0] == "grace" and calls == 1:
            raise ConnectionError("database went away after Slack took the DM")
        return ref

    async with services.db.session() as session:
        user = await session.get(UserRow, "grace")
        assert user is not None
        user.timezone, user.standup_time = "UTC", "09:00"
        await session.commit()
    monkeypatch.setattr(chat, "dm", dm_then_fail)
    for minute in (0, 1):
        await run_due_standups(services, now=datetime(2026, 6, 15, 9, minute, tzinfo=UTC))
    grace_dms = [d for d in chat.dms if d["user_id"] == "grace"]  # type: ignore[attr-defined]
    assert len(grace_dms) == 2
    async with services.db.session() as session:  # the ticks ran on a past day; click in time
        for row in await session.scalars(select(ApprovalRow)):
            row.expires_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()

    def click(dm: dict[str, Any]) -> dict[str, Any]:
        return {
            "actions": [{"value": dm["blocks"][1]["elements"][0]["value"]}],
            "user": {"id": "UGRACE"},
            "channel": {"id": "DGRACE"},
            "message": {"ts": "1.000"},
        }

    client = FakeSlackClient()
    await post_standup_from_draft(services, click(grace_dms[0]), client)
    assert await _standups_in_channel(services) == []
    assert "no longer valid" in client.updates[-1]["text"]
    await post_standup_from_draft(services, click(grace_dms[1]), client)
    assert len(await _standups_in_channel(services)) == 1
