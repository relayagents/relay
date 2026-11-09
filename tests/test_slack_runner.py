"""The Socket Mode runner must never hold up API startup.

First compose boot: the placeholder tokens from .env.example made Slack answer invalid_auth, and
slack_sdk's connect_async() retries that forever, so the API never finished starting and /health
never answered. A bad token has to fail fast (the lifespan logs it and runs without Slack), and a
slow or unreachable Slack has to connect in the background.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from relayagents.api.slack.app import SlackRunner
from relayagents.tools.context import Services


def _response(data: dict[str, Any]) -> AsyncSlackResponse:
    return AsyncSlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/test",
        req_args={},
        data=data,
        headers={},
        status_code=200,
    )


def _invalid_auth(*args: Any, **kwargs: Any) -> Any:
    raise SlackApiError("invalid_auth", _response({"ok": False, "error": "invalid_auth"}))


async def _ok(*args: Any, **kwargs: Any) -> AsyncSlackResponse:
    return _response({"ok": True, "url": "wss://example.invalid/link"})


@pytest.fixture
def slack_services(services: Services) -> Services:
    services.settings = services.settings.model_copy(
        update={"slack_bot_token": "xoxb-test", "slack_app_token": "xapp-test"}
    )
    return services


@pytest.mark.parametrize("bad", ["auth_test", "apps_connections_open"])
async def test_bad_token_fails_fast_instead_of_retrying_forever(
    slack_services: Services, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setattr(AsyncWebClient, "auth_test", _ok)
    monkeypatch.setattr(AsyncWebClient, "apps_connections_open", _ok)
    monkeypatch.setattr(AsyncWebClient, bad, _invalid_auth)
    runner = SlackRunner(slack_services)
    with pytest.raises(SlackApiError, match="invalid_auth"):
        await asyncio.wait_for(runner.start(), timeout=5)
    await runner.stop()
    # The lifespan stops a runner that failed to start; that must release the socket client's
    # aiohttp session, or every boot with a bad token logs "Unclosed client session".
    session = runner.handler.client.aiohttp_client_session
    assert session is None or session.closed


async def test_unreachable_slack_keeps_retrying_in_the_background(
    slack_services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(*args: Any, **kwargs: Any) -> Any:
        raise OSError("network is unreachable")

    monkeypatch.setattr(AsyncWebClient, "auth_test", offline)
    connecting = asyncio.Event()

    async def connect() -> None:
        connecting.set()

    runner = SlackRunner(slack_services)
    monkeypatch.setattr(runner.handler, "connect_async", connect)
    await asyncio.wait_for(runner.start(), timeout=5)
    await asyncio.wait_for(connecting.wait(), timeout=5)
    await runner.stop()


async def test_slow_connect_does_not_block_startup(
    slack_services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AsyncWebClient, "auth_test", _ok)
    monkeypatch.setattr(AsyncWebClient, "apps_connections_open", _ok)
    connecting = asyncio.Event()

    async def hang() -> None:
        connecting.set()
        await asyncio.Event().wait()  # Slack unreachable: slack_sdk keeps retrying

    runner = SlackRunner(slack_services)
    monkeypatch.setattr(runner.handler, "connect_async", hang)
    await asyncio.wait_for(runner.start(), timeout=5)
    await asyncio.wait_for(connecting.wait(), timeout=5)
    await asyncio.wait_for(runner.stop(), timeout=5)
