from __future__ import annotations

import httpx

from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
from relayagents.core.events import Actor
from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth


async def test_agents_register_themselves_not_add_user(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    # The fixture registered one agent per user, as `relay setup-agent` does.
    r = await client.get("/a2a/agents/grace.claude-code/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "grace.claude-code"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a/agents/grace.claude-code")
    listed = (await client.get("/a2a/agents", headers=auth(team["ada"]["human"]))).json()
    assert {a["agent_id"] for a in listed} == {
        "ada.claude-code",
        "grace.claude-code",
        "linus.claude-code",
    }
    # add-user mints the human token only (ADR-0011): no agent token, no AgentCard.
    r = await client.post(
        "/v1/users", json={"id": "mallory", "display_name": "M"}, headers=auth(team["ada"]["human"])
    )
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"user", "human_token", "relay_url"}
    listed = (await client.get("/a2a/agents", headers=auth(team["ada"]["human"]))).json()
    assert not any(a["agent_id"].startswith("mallory.") for a in listed)


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
    assert ask["to_agent"] == "grace.claude-code" and ask["state"] == "submitted"
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
    r = await client.post(
        "/a2a/agents/linus.claude-code", json=body, headers=auth(team["ada"]["agent"])
    )
    assert r.status_code == 200
    task = r.json()["result"]
    assert task["status"]["state"] == "submitted" and task["metadata"]["to"] == "linus.claude-code"
    got = await client.post(
        "/a2a/agents/linus.claude-code",
        json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}},
        headers=auth(team["ada"]["agent"]),
    )
    assert got.json()["result"]["id"] == task["id"]
    bad = await client.post(
        "/a2a/agents/nobody.claude-code", json=body, headers=auth(team["ada"]["agent"])
    )
    assert "unknown agent" in bad.json()["error"]["message"]


async def test_ask_unknown_user_is_a_clean_error(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "nobody", "question": "?"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 400 and "unknown user" in r.json()["detail"]


async def test_ask_without_an_agent_reaches_the_human(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    await create_user_with_tokens(
        services,
        AddUserIn(id="nora", display_name="Nora", slack_user_id="UNORA"),
        issued_by=Actor.system("relay.test"),
    )
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "nora", "question": "Which GPU partition?"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to_agent"] is None and body["task_id"] is None
    assert body["state"] == "human_only"
    dm = services.chat.dms[-1]
    assert dm["user_id"] == "nora" and "asked you" in dm["text"] and "GPU partition" in dm["text"]
    evs = (
        await client.get(
            "/v1/events", params={"thread": body["thread_id"]}, headers=auth(team["ada"]["human"])
        )
    ).json()
    assert [e["type"] for e in evs] == ["question.opened"]


async def test_ask_records_the_question_even_when_the_dm_fails(  # type: ignore[no-untyped-def]
    client: httpx.AsyncClient, team, services, monkeypatch
) -> None:
    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise LookupError("user has no slack_user_id")

    monkeypatch.setattr(services.chat, "dm", boom)
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "grace", "question": "Still on it?"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.status_code == 200, r.text
    evs = (
        await client.get(
            "/v1/events",
            params={"thread": r.json()["thread_id"]},
            headers=auth(team["ada"]["human"]),
        )
    ).json()
    assert "question.opened" in [e["type"] for e in evs]


async def test_the_most_recently_seen_agent_gets_the_task(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    # grace adds a second agent; it is the newest, so it is the one asks go to
    r = await client.post(
        "/v1/tokens",
        json={"actor_kind": "agent", "harness": "codex", "label": "agent:codex"},
        headers=auth(team["grace"]["human"]),
    )
    codex = auth(r.json()["token"])
    r = await client.post(
        "/a2a/agents", json={"harness": "codex", "card": {"name": "grace.codex"}}, headers=codex
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post(
        f"{TOOLS_PREFIX}/ask",
        json={"user": "grace", "question": "Which branch?"},
        headers=auth(team["ada"]["agent"]),
    )
    assert r.json()["to_agent"] == "grace.codex"
