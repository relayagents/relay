"""Slack Bolt (async) app: approval buttons, login approvals, standup draft buttons."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select

from relayagents.core import approvals
from relayagents.core.models import ApprovalRow, UserRow
from relayagents.tools.context import Services

log = structlog.get_logger()


async def _user_id_for_slack(services: Services, slack_user_id: str) -> str | None:
    async with services.db.session() as session:
        row = await session.scalar(select(UserRow).where(UserRow.slack_user_id == slack_user_id))
        return row.id if row else None


def build_slack_app(services: Services) -> AsyncApp:
    app = AsyncApp(token=services.settings.slack_bot_token)

    @app.action("approval_approve")
    async def on_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await _resolve_from_slack(services, body, client, "approved")

    @app.action("approval_deny")
    async def on_deny(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await _resolve_from_slack(services, body, client, "denied")

    @app.action("login_approve")
    async def on_login_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await _login_from_slack(services, body, client, approved=True)

    @app.action("login_deny")
    async def on_login_deny(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await _login_from_slack(services, body, client, approved=False)

    @app.action("standup_approve")
    async def on_standup_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        from relayagents.workers.standup import post_standup_from_draft

        await post_standup_from_draft(services, body, client)

    @app.action("standup_edit")
    async def on_standup_edit(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        from relayagents.workers.standup import open_standup_edit_modal

        await open_standup_edit_modal(services, body, client)

    @app.view("standup_edit_submit")
    async def on_standup_edit_submit(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        from relayagents.workers.standup import post_standup_from_modal

        await post_standup_from_modal(services, body, client)

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], say: Any) -> None:
        await say("I'm Relay. Your agents talk to me; ask them, or run `relay --help`.")

    return app


async def _resolve_from_slack(
    services: Services, body: dict[str, Any], client: Any, decision: str
) -> None:
    approval_id = body["actions"][0]["value"]
    slack_user = body["user"]["id"]
    user_id = await _user_id_for_slack(services, slack_user)
    async with services.db.session() as session:
        row = await session.get(ApprovalRow, approval_id)
        if row is None:
            return
        if user_id is None or user_id != row.requested_of:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=slack_user,
                text="Only the person this approval was requested of can resolve it.",
            )
            return
        was = row.status
        row = await approvals.resolve(
            session, approval_id=approval_id, decision=decision, resolved_by=user_id
        )
        await session.commit()
    if was != "pending" or row.status != decision:
        # The click changed nothing: say so, and show what was actually recorded below.
        with contextlib.suppress(Exception):
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=slack_user,
                text=f"Nothing was done: this approval is already {row.status}.",
            )
    # Render the recorded outcome, never the button that was pressed.
    text, blocks = approvals.settled_message(
        row, by=f"<@{slack_user}>" if row.resolved_by == user_id else None
    )
    with contextlib.suppress(Exception):
        await client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=blocks
        )


async def _login_from_slack(
    services: Services, body: dict[str, Any], client: Any, *, approved: bool
) -> None:
    from relayagents.api.routes.users import approve_device

    device_code = body["actions"][0]["value"]
    slack_user = body["user"]["id"]
    user_id = await _user_id_for_slack(services, slack_user)
    try:
        row = await approve_device(services, device_code, approved=approved, by_user=user_id)
    except (KeyError, PermissionError) as exc:
        await client.chat_postEphemeral(
            channel=body["channel"]["id"], user=slack_user, text=f"Cannot approve: {exc}"
        )
        return
    with contextlib.suppress(Exception):
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"Login {row.user_code}: {row.status}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":key: Login *{row.user_code}* {row.status} by <@{slack_user}>",
                    },
                }
            ],
        )


# Slack error codes that mean the token itself will never work: retrying cannot fix them.
TOKEN_REJECTED = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_allowed_token_type",
        "missing_scope",
    }
)


class SlackRunner:
    """Runs the Socket Mode connection inside the API process lifespan."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.app = build_slack_app(services)
        self.handler = AsyncSocketModeHandler(self.app, services.settings.slack_app_token)
        self._connecting: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Check both tokens once, then connect in the background.

        slack_sdk's ``connect_async()`` retries every failure forever, ``invalid_auth`` included,
        so awaiting it here let a bad token or a Slack outage hang API startup and ``/health``.
        A token Slack rejects raises from here; the lifespan logs it and runs without Slack.
        Anything else (Slack unreachable, rate limited, or having a bad moment) is not a verdict
        on the tokens, so that keeps retrying in the background like any later disconnect.
        """
        settings = self.services.settings
        try:
            await AsyncWebClient(token=settings.slack_bot_token).auth_test()
            await AsyncWebClient().apps_connections_open(app_token=settings.slack_app_token)
        except SlackApiError as exc:
            if exc.response.get("error") in TOKEN_REJECTED:
                raise
            log.warning("slack.unavailable_at_start", error=str(exc))
        except Exception as exc:
            log.warning("slack.unreachable_at_start", error=str(exc))
        self._connecting = asyncio.create_task(self._connect(), name="slack-socket-mode")

    async def _connect(self) -> None:
        try:
            await self.handler.connect_async()
        except Exception as exc:
            log.warning("slack.socket_mode.connect_failed", error=str(exc))
            return
        log.info("slack.socket_mode.connected")

    async def stop(self) -> None:
        if self._connecting is not None:
            self._connecting.cancel()
            with contextlib.suppress(BaseException):
                await self._connecting
        with contextlib.suppress(Exception):
            await self.handler.close_async()
