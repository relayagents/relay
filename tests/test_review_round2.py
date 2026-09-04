"""Regression tests for the second self-review round."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
from relayagents.connectors.memory.embeddings import PydanticAIEmbedder, embed_events
from relayagents.connectors.slack.chat import RecordingChatApp
from relayagents.core.events import Actor
from relayagents.core.models import EMBEDDING_DIM
from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth


async def test_add_user_slack_clash_is_409_and_reissue_updates_bindings(services, team) -> None:  # type: ignore[no-untyped-def]
    from fastapi import HTTPException

    who = Actor.system("relay.test")
    with pytest.raises(HTTPException) as exc:
        await create_user_with_tokens(
            services, AddUserIn(id="bob", display_name="Bob", slack_user_id="UGRACE"), issued_by=who
        )
    assert exc.value.status_code == 409
    out = await create_user_with_tokens(
        services,
        AddUserIn(
            id="grace",
            display_name="Grace",
            slack_user_id="UGRACE2",
            github_login="gh-grace",
            reissue=True,
        ),
        issued_by=who,
    )
    assert out.user.slack_user_id == "UGRACE2" and out.user.github_login == "gh-grace"


async def test_non_interactive_chat_gets_cli_instructions_not_buttons(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    services.chat = RecordingChatApp(supports_actions=False)
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={"action": "x", "wait": False},
        headers=auth(team["ada"]["agent"]),
    )
    blocks = services.chat.dms[-1]["blocks"]
    assert all(b["type"] != "actions" for b in blocks)
    assert "relay approvals approve" in blocks[-1]["elements"][0]["text"]
    d = (await client.post("/v1/auth/device", json={"user_id": "grace", "label": "cli"})).json()
    assert d["slack"] is False and not any("Login request" in m["text"] for m in services.chat.dms)
    # the CLI path still works for the human
    r = await client.post(
        f"/v1/approvals/{r.json()['approval_id']}/resolve",
        json={"decision": "approved"},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 200


async def test_approval_action_text_is_redacted(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={
            "action": "call x with Authorization: Bearer sk-abcdefghijklmnopqrstuv",
            "wait": False,
        },
        headers=auth(team["ada"]["agent"]),
    )
    listed = (await client.get("/v1/approvals", headers=auth(team["ada"]["human"]))).json()
    assert "sk-" not in listed[0]["action"] and "***" in listed[0]["action"]
    rep = await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "used token rly_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 today"},
        headers=auth(team["ada"]["agent"]),
    )
    ev = (
        await client.get(f"/v1/events/{rep.json()['event_id']}", headers=auth(team["ada"]["human"]))
    ).json()
    assert "rly_" not in ev["payload"]["text"]


async def test_bad_transcript_leaves_no_orphaned_files(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        "/v1/meetings",
        data={"title": "x"},
        files={"transcript": ("t.json", b"not json at all", "application/json")},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 422
    meetings_dir = services.settings.data_dir / "meetings"
    assert not meetings_dir.exists() or not any(meetings_dir.iterdir())


class FlakyModel:
    """Fails whole batches that contain a poison input, like a provider 400."""

    model_name = "flaky"

    async def embed_documents(self, texts: Sequence[str]):  # type: ignore[no-untyped-def]
        class R:
            embeddings = [[1.0] * EMBEDDING_DIM for _ in texts]

        if any("POISON" in t for t in texts):
            raise RuntimeError("400 bad input")
        return R()


async def test_embedder_falls_back_per_item_on_batch_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    emb = PydanticAIEmbedder.__new__(PydanticAIEmbedder)
    emb.model_name = "flaky"
    emb._embedder = FlakyModel()  # type: ignore[assignment]
    vecs = await emb.embed_documents(["fine", "POISON", "also fine", "x" * 20000])
    assert [v is None for v in vecs] == [False, True, False, False]
    assert len(vecs[3]) == EMBEDDING_DIM  # type: ignore[arg-type]


async def test_embed_events_skips_rejected_inputs(services) -> None:  # type: ignore[no-untyped-def]
    from relayagents.core.events import DecisionMade, Event
    from relayagents.core.store import EventStore

    async with services.db.session() as session:
        a = await EventStore(session).append(
            Event.new(
                DecisionMade(decision_id="a", statement="fine"),
                actor=Actor.human("ada"),
                source="meeting",
            )
        )
        b = await EventStore(session).append(
            Event.new(
                DecisionMade(decision_id="b", statement="POISON"),
                actor=Actor.human("ada"),
                source="meeting",
            )
        )
        await session.commit()
    emb = PydanticAIEmbedder.__new__(PydanticAIEmbedder)
    emb.model_name = "flaky"
    emb._embedder = FlakyModel()  # type: ignore[assignment]
    assert await embed_events(services.db, emb, [a, b]) == 1


async def test_new_events_are_queued_for_indexing(
    client: httpx.AsyncClient, team, services
) -> None:  # type: ignore[no-untyped-def]
    class Queue:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, tuple]] = []

        async def enqueue_job(self, name: str, *args, **kw):  # type: ignore[no-untyped-def]
            self.jobs.append((name, args))

    services.queue = Queue()
    await client.post(
        f"{TOOLS_PREFIX}/report", json={"text": "indexed?"}, headers=auth(team["ada"]["agent"])
    )
    await client.post(
        "/v1/events",
        json={"payload": {"type": "decision.made", "decision_id": "d", "statement": "s"}},
        headers=auth(team["ada"]["human"]),
    )
    assert [j[0] for j in services.queue.jobs] == ["index_events", "index_events"]
    assert all(len(j[1][0]) == 1 and j[1][0][0].startswith("evt_") for j in services.queue.jobs)


async def test_agents_cannot_resolve_via_rest(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        f"{TOOLS_PREFIX}/request_approval",
        json={"action": "x", "wait": False},
        headers=auth(team["grace"]["agent"]),
    )
    assert (
        await client.post(
            f"/v1/approvals/{r.json()['approval_id']}/resolve",
            json={"decision": "approved"},
            headers=auth(team["grace"]["agent"]),
        )
    ).status_code == 403
