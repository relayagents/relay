from __future__ import annotations

import httpx

from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth


async def test_agent_cards_are_registered_on_add_user(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.get("/a2a/agents/grace.hermes/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "grace.hermes"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a/agents/grace.hermes")
    listed = (await client.get("/a2a/agents", headers=auth(team["ada"]["human"]))).json()
    assert {a["agent_id"] for a in listed} == {"ada.hermes", "grace.hermes", "linus.hermes"}


async def test_ask_flows_through_the_broker(client: httpx.AsyncClient, team, services) -> None:  # type: ignore[no-untyped-def]
    ada_agent, grace_agent = team["ada"]["agent"], team["grace"]["agent"]
    # ada's agent asks grace's agent
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "@grace", "question": "Is the cache table merged?"},
        headers=auth(ada_agent),
    )
    assert r.status_code == 200, r.text
    ask = r.json()
    assert ask["to_agent"] == "grace.hermes" and ask["state"] == "submitted"
    # grace's human was told (principle 7)
    assert any(
        d["user_id"] == "grace" and "asked your agent" in d["text"] for d in services.chat.dms
    )
    # grace's agent long-polls its inbox
    inbox = (await client.get("/a2a/inbox", params={"wait": 0}, headers=auth(grace_agent))).json()
    assert [t["id"] for t in inbox] == [ask["task_id"]]
    assert inbox[0]["history"][0]["parts"][0]["text"] == "Is the cache table merged?"
    # a human token cannot read an agent inbox
    assert (await client.get("/a2a/inbox", headers=auth(team["grace"]["human"]))).status_code == 403
    # grace's agent answers
    r = await client.post(
        f"/a2a/tasks/{ask['task_id']}",
        json={
            "state": "completed",
            "message": {"role": "agent", "parts": [{"text": "Merged this morning."}]},
        },
        headers=auth(grace_agent),
    )
    assert r.status_code == 200 and r.json()["status"]["state"] == "completed"
    # ada's human was told of the outcome
    assert any(
        d["user_id"] == "ada" and "Merged this morning" in d["text"] for d in services.chat.dms
    )
    # everything is events, threaded
    evs = (
        await client.get("/v1/events", params={"thread": ask["thread_id"]}, headers=auth(ada_agent))
    ).json()
    assert [e["type"] for e in evs] == ["agent.message", "question.opened", "agent.message"]
    assert evs[-1]["payload"]["state"] == "completed"


async def test_jsonrpc_message_send(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {"role": "user", "parts": [{"text": "hello from a generic A2A client"}]}
        },
    }
    r = await client.post("/a2a/agents/linus.hermes", json=body, headers=auth(team["ada"]["agent"]))
    assert r.status_code == 200
    task = r.json()["result"]
    assert task["status"]["state"] == "submitted" and task["metadata"]["to"] == "linus.hermes"
    got = await client.post(
        "/a2a/agents/linus.hermes",
        json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}},
        headers=auth(team["ada"]["agent"]),
    )
    assert got.json()["result"]["id"] == task["id"]
    bad = await client.post(
        "/a2a/agents/nobody.hermes", json=body, headers=auth(team["ada"]["agent"])
    )
    assert "unknown agent" in bad.json()["error"]["message"]


async def test_ask_unknown_user_is_a_clean_error(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "nobody", "question": "?"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 400 and "no agent registered" in r.json()["detail"]
