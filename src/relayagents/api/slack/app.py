"""Slack Bolt (async) app: approval buttons, login approvals, standup draft buttons."""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
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
        row = await approvals.resolve(
            session, approval_id=approval_id, decision=decision, resolved_by=user_id
        )
        await session.commit()
    verdict = ":white_check_mark: Approved" if decision == "approved" else ":no_entry: Denied"
    with contextlib.suppress(Exception):
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"{verdict} by <@{slack_user}>: {row.action}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{verdict} by <@{slack_user}>\n> {row.action}\n`{approval_id}`",
                    },
                }
            ],
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


class SlackRunner:
    """Runs the Socket Mode connection inside the API process lifespan."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.app = build_slack_app(services)
        self.handler = AsyncSocketModeHandler(self.app, services.settings.slack_app_token)

    async def start(self) -> None:
        await self.handler.connect_async()
        log.info("slack.socket_mode.connected")

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.handler.close_async()
