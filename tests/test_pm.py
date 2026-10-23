"""PM dispatch reaches the human first; an agent gets the task too when one is registered (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
from relayagents.core.events import ActionItemCreated, Actor, Event
from relayagents.core.models import MeetingRow
from relayagents.tools.context import Services
from relayagents.workers import pm


async def _dispatch_to(services: Services, assignee: str) -> dict[str, Any]:
    meeting = MeetingRow(
        id="mtg_pm",
        title="Sync",
        status="done",
        participants=[assignee],
        created_by=assignee,
        created_at=datetime.now(UTC),
    )
    ev = Event.new(
        ActionItemCreated(
            item_id="item_pm", title="Write the eval doc", assignee=assignee, meeting_id="mtg_pm"
        ),
        actor=Actor.system("relay.test"),
        source="meeting",
        thread_id="mtg_pm",
    )
    async with services.db.session() as session:
        session.add(meeting)
        await session.flush()
        out = await pm.dispatch(session, services, meeting, [ev])
        await session.commit()
    return out


async def _add_nora(services: Services) -> None:
    await create_user_with_tokens(
        services,
        AddUserIn(id="nora", display_name="Nora", slack_user_id="UNORA"),
        issued_by=Actor.system("relay.test"),
    )


async def test_dispatch_dms_the_assignee_without_an_agent(services: Services) -> None:
    await _add_nora(services)
    out = await _dispatch_to(services, "nora")
    assert out["tasks"] == []
    assert out["human_only"] == [{"item_id": "item_pm", "assignee": "nora"}]
    dm = services.chat.dms[-1]  # type: ignore[union-attr]
    assert dm["user_id"] == "nora"
    assert "Write the eval doc" in dm["text"] and "item_pm" in dm["text"]
    assert "my_items" in dm["text"]  # tells them how their coding agent will see it


async def test_dispatch_without_slack_still_reports_human_only(services: Services) -> None:
    await _add_nora(services)
    services.chat = None
    out = await _dispatch_to(services, "nora")
    assert out["summary_posted"] is False
    assert out["human_only"] == [{"item_id": "item_pm", "assignee": "nora"}]


async def test_dispatch_also_hands_the_item_to_a_registered_agent(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    out = await _dispatch_to(services, "grace")
    assert out["human_only"] == []
    (task,) = out["tasks"]
    assert task["to"] == "grace.claude-code" and task["item_id"] == "item_pm"
    dm = services.chat.dms[-1]  # type: ignore[union-attr]
    assert dm["user_id"] == "grace" and "Write the eval doc" in dm["text"]
    assert f"Your agent also has it as task `{task['task_id']}`" in dm["text"]
