from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from relayagents.api.slack.app import _resolve_from_slack
from relayagents.core.models import ApprovalRow
from relayagents.core.permissions import DEFAULT_POLICY, policy_for
from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth


def test_policy_defaults() -> None:
    assert policy_for("github.issue.create") == "approve"
    assert policy_for("slack.post.as_user") == "forbid"
    assert policy_for("relay.report") == "auto"
    assert policy_for("something.new") == "approve"  # unknown → approve
    assert set(DEFAULT_POLICY.values()) <= {"auto", "approve", "forbid"}


async def test_request_approval_round_trip(client: httpx.AsyncClient, team, services) -> None:  # type: ignore[no-untyped-def]
    agent, human = team["grace"]["agent"], team["grace"]["human"]
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={
            "action": "open GitHub issue for the cache table",
            "action_type": "github.issue.create",
            "wait": False,
        },
        headers=auth(agent),
    )
    assert r.status_code == 200, r.text
    ap = r.json()
    assert ap["status"] == "pending" and ap["requested_of"] == "grace"
    dm = services.chat.dms[-1]
    assert dm["user_id"] == "grace" and dm["blocks"][1]["elements"][0]["value"] == ap["approval_id"]

    # another human cannot resolve it
    r = await client.post(
        f"/v1/approvals/{ap['approval_id']}/resolve",
        json={"decision": "approved"},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 403
    # an agent cannot resolve it
    r = await client.post(
        f"/v1/approvals/{ap['approval_id']}/resolve",
        json={"decision": "approved"},
        headers=auth(agent),
    )
    assert r.status_code == 403
    # grace can
    r = await client.post(
        f"/v1/approvals/{ap['approval_id']}/resolve",
        json={"decision": "approved", "edited_action": "open GitHub issue in relayagents/relay"},
        headers=auth(human),
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"

    evs = (
        await client.get("/v1/events", params={"thread": ap["approval_id"]}, headers=auth(human))
    ).json()
    assert [e["type"] for e in evs] == ["approval.requested", "approval.resolved"]
    assert evs[1]["payload"]["edited_action"] == "open GitHub issue in relayagents/relay"
    assert evs[1]["actor"] == {"kind": "human", "id": "grace"}


async def test_request_approval_blocks_until_resolved(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    agent, human = team["linus"]["agent"], team["linus"]["human"]

    async def resolve_soon() -> None:
        await asyncio.sleep(0.3)
        pending = (await client.get("/v1/approvals", headers=auth(human))).json()
        assert len(pending) == 1
        await client.post(
            f"/v1/approvals/{pending[0]['id']}/resolve",
            json={"decision": "denied"},
            headers=auth(human),
        )

    task = asyncio.create_task(resolve_soon())
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={
            "action": "run coding agent",
            "action_type": "coding_agent.run",
            "wait": True,
            "timeout_s": 10,
        },
        headers=auth(agent),
    )
    await task
    assert r.json()["status"] == "denied"


async def test_forbidden_action_is_rejected(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={"action": "send mail", "action_type": "workspace.mail.send"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 400 and "forbidden" in r.json()["detail"]


async def test_post_as_human_is_forbidden(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/post",
        json={"text": "hi", "as_agent": False},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 400 and "forbidden" in r.json()["detail"]


async def test_post_as_agent_is_attributed_and_audited(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/post",
        json={"text": "cache table is in"},
        headers=auth(team["grace"]["agent"]),
    )
    assert r.status_code == 200, r.text
    assert services.chat.posts[-1]["attribution"] == "posted by Grace's agent"
    evs = (
        await client.get(
            "/v1/events",
            params={"type": ["tool.called", "tool.result"]},
            headers=auth(team["grace"]["human"]),
        )
    ).json()
    assert evs[-2]["payload"]["target"] == "slack:C_TEAM" and evs[-1]["payload"]["ok"] is True


class _SlackClient:
    """Records the two Slack calls the approval handler makes."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    async def chat_update(self, **kw) -> None:  # type: ignore[no-untyped-def]
        self.updates.append(kw)

    async def chat_postEphemeral(self, **kw) -> None:  # type: ignore[no-untyped-def]  # noqa: N802 (Slack's name)
        self.ephemerals.append(kw)


async def _open(client: httpx.AsyncClient, token: str, **kw) -> dict:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={"action": "open an issue", "action_type": "github.issue.create", "wait": False, **kw},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _expire(services, approval_id: str) -> None:  # type: ignore[no-untyped-def]
    async with services.db.session() as session:
        row = await session.get(ApprovalRow, approval_id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


def _click(approval_id: str, slack_user: str) -> dict:
    return {
        "actions": [{"value": approval_id}],
        "user": {"id": slack_user},
        "channel": {"id": "DGRACE"},
        "message": {"ts": "1.000"},
    }


async def test_a_late_answer_does_not_count(client: httpx.AsyncClient, team, services) -> None:  # type: ignore[no-untyped-def]
    # Nobody waited (wait=False), so expiry was never written; the deadline still holds.
    ap = await _open(client, team["grace"]["agent"])
    await _expire(services, ap["approval_id"])
    r = await client.post(
        f"/v1/approvals/{ap['approval_id']}/resolve",
        json={"decision": "approved"},
        headers=auth(team["grace"]["human"]),
    )
    assert r.status_code == 200 and r.json()["status"] == "expired"
    assert r.json()["resolved_by"] is None
    evs = (
        await client.get(
            "/v1/events", params={"thread": ap["approval_id"]}, headers=auth(team["grace"]["human"])
        )
    ).json()
    assert evs[-1]["payload"]["decision"] == "expired"
    assert evs[-1]["actor"] == {"kind": "system", "id": "relay.approvals"}
    # The DM's buttons are replaced, so the Slack message says what happened.
    update = services.chat.posts[-1]
    assert update["update"] and update["text"].startswith(":hourglass: Expired")


async def test_slack_shows_the_recorded_outcome_not_the_button(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    ap = await _open(client, team["grace"]["agent"])
    await _expire(services, ap["approval_id"])
    slack = _SlackClient()
    await _resolve_from_slack(services, _click(ap["approval_id"], "UGRACE"), slack, "approved")
    assert "Approved" not in slack.updates[-1]["text"]
    assert slack.updates[-1]["text"].startswith(":hourglass: Expired")
    assert "already expired" in slack.ephemerals[-1]["text"]

    # A second click on a settled approval cannot flip what the message says either.
    ap = await _open(client, team["grace"]["agent"])
    slack = _SlackClient()
    await _resolve_from_slack(services, _click(ap["approval_id"], "UGRACE"), slack, "denied")
    await _resolve_from_slack(services, _click(ap["approval_id"], "UGRACE"), slack, "approved")
    assert [u["text"].split(" by ")[0] for u in slack.updates] == [":no_entry: Denied"] * 2
    assert "already denied" in slack.ephemerals[-1]["text"]
    assert len(slack.ephemerals) == 1


async def test_an_approval_that_times_out_retires_its_buttons(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={"action": "open an issue", "action_type": "github.issue.create", "timeout_s": 1},
        headers=auth(team["linus"]["agent"]),
    )
    assert r.json()["status"] == "expired"
    update = services.chat.posts[-1]
    assert update["update"] and update["text"].startswith(":hourglass: Expired")
