"""MCP, CLI, and REST are generated from one registry and must never drift."""

from __future__ import annotations

import httpx
import pytest
from click import Context
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from typer.main import get_command

from relayagents.cli.main import app as cli_app
from relayagents.tools import TOOLS
from relayagents.tools.rest import TOOLS_PREFIX
from tests.conftest import auth

EXPECTED = {
    "recall",
    "my_items",
    "items",
    "events",
    "report",
    "ask",
    "request_approval",
    "decisions",
    "post",
}


def test_registry_names() -> None:
    assert {t.name for t in TOOLS} == EXPECTED
    for t in TOOLS:
        assert t.description and t.input_model and t.output_model


async def test_mcp_tools_match_registry(app) -> None:  # type: ignore[no-untyped-def]
    tools = {t.name: t for t in await app.state.mcp.list_tools()}
    assert set(tools) == EXPECTED
    for spec in TOOLS:
        schema = spec.input_model.model_json_schema()
        mcp_schema = tools[spec.name].input_schema
        assert set(mcp_schema["properties"]) == set(schema["properties"]), spec.name
        assert set(mcp_schema.get("required", [])) == set(schema.get("required", [])), spec.name
        for name, prop in schema["properties"].items():
            if "description" in prop:
                assert mcp_schema["properties"][name].get("description") == prop["description"], (
                    spec.name,
                    name,
                )
        out = tools[spec.name].output_schema
        assert out is not None and set(out["properties"]) == set(
            spec.output_model.model_json_schema()["properties"]
        ), spec.name
        assert tools[spec.name].annotations.read_only_hint == spec.read_only


def test_rest_routes_match_registry(app) -> None:  # type: ignore[no-untyped-def]
    paths = app.openapi()["paths"]
    for spec in TOOLS:
        op = paths[f"{TOOLS_PREFIX}/{spec.name}"]["post"]
        body_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert body_ref.endswith(spec.input_model.__name__)
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            spec.output_model.__name__
        )


def test_cli_commands_match_registry() -> None:
    cmd = get_command(cli_app)
    for spec in TOOLS:
        sub = cmd.commands[spec.name.replace("_", "-")]
        params = {p.name for p in sub.params}
        assert set(spec.input_model.model_fields) <= params, spec.name
        assert "json_output" in params
        # positional args are exactly what the spec says
        args = [p.name for p in sub.params if p.param_type_name == "argument"]
        assert args == list(spec.positional), spec.name
        Context(sub).get_help()  # renders without error


async def test_rest_and_mcp_return_the_same_thing(app, client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    tok = team["grace"]["agent"]
    r = await client.post(
        f"{TOOLS_PREFIX}/report",
        json={"text": "wired the cache", "link": "https://example.com/pr/1"},
        headers=auth(tok),
    )
    assert r.status_code == 200, r.text
    rest_events = (
        await client.post(
            f"{TOOLS_PREFIX}/events", json={"since": "1h", "actor": "me"}, headers=auth(tok)
        )
    ).json()["events"]
    assert [e["type"] for e in rest_events] == ["report.posted"]

    async with (
        app.state.mcp.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=auth(tok)
        ) as http,
    ):
        async with (
            streamable_http_client("http://testserver/mcp", http_client=http) as (read, write, *_),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            res = await session.call_tool("events", {"since": "1h", "actor": "me"})
            assert not res.is_error, res
            mcp_events = res.structured_content["events"]
    assert [e["id"] for e in mcp_events] == [e["id"] for e in rest_events]
    assert mcp_events[0]["actor"] == {"kind": "agent", "id": "grace.hermes"}


async def test_mcp_rejects_bad_token(app) -> None:  # type: ignore[no-untyped-def]
    async with (
        app.state.mcp.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=auth("rly_nope"),
        ) as http,
    ):
        with pytest.raises(Exception):  # noqa: B017 - transport raises on 401
            async with (
                streamable_http_client("http://testserver/mcp", http_client=http) as (
                    read,
                    write,
                    *_,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()


async def test_rest_requires_token(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"{TOOLS_PREFIX}/my_items", json={})).status_code == 401


async def test_state_changing_tools_are_audited(client: httpx.AsyncClient, team) -> None:  # type: ignore[no-untyped-def]
    tok = team["ada"]["human"]
    await client.post(f"{TOOLS_PREFIX}/report", json={"text": "did a thing"}, headers=auth(tok))
    evs = (
        await client.get(
            "/v1/events", params={"type": ["tool.called", "tool.result"]}, headers=auth(tok)
        )
    ).json()
    assert [e["type"] for e in evs] == ["tool.called", "tool.result"]
    assert evs[1]["provenance"]["parent_event_ids"] == [evs[0]["id"]]
    # hidden from the default events view
    shown = (
        await client.post(f"{TOOLS_PREFIX}/events", json={"since": "1h"}, headers=auth(tok))
    ).json()["events"]
    assert all(not e["type"].startswith("tool.") for e in shown)
