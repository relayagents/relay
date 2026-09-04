"""``ChatApp`` implementation on the Slack Web API (bot token from the single Relay app)."""

from __future__ import annotations

from typing import Any

from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select

from relayagents.core.db import Database
from relayagents.core.models import UserRow
from relayagents.core.protocols import ChatMessageRef


class SlackChatApp:
    def __init__(self, bot_token: str, db: Database) -> None:
        self.client = AsyncWebClient(token=bot_token)
        self.db = db
        self._dm_cache: dict[str, str] = {}

    async def _slack_user(self, user_id: str) -> str:
        async with self.db.session() as session:
            row = await session.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None or not row.slack_user_id:
            raise LookupError(
                f"user {user_id!r} has no slack_user_id; set it with `relay me --slack-user-id U...`"
            )
        return row.slack_user_id

    async def _dm_channel(self, user_id: str) -> str:
        if user_id in self._dm_cache:
            return self._dm_cache[user_id]
        slack_user = await self._slack_user(user_id)
        resp = await self.client.conversations_open(users=[slack_user])
        channel = resp["channel"]["id"]
        self._dm_cache[user_id] = channel
        return channel

    async def post(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        attribution: str | None = None,
    ) -> ChatMessageRef:
        body = text if not attribution else f"{text}\n_{attribution}_"
        blks = blocks
        if attribution and blocks is None:
            blks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f":robot_face: {attribution}"}],
                },
            ]
        resp = await self.client.chat_postMessage(channel=channel, text=body, blocks=blks)
        return ChatMessageRef(channel=resp["channel"], ts=resp["ts"])

    async def dm(
        self, user_id: str, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> ChatMessageRef:
        channel = await self._dm_channel(user_id)
        resp = await self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return ChatMessageRef(channel=resp["channel"], ts=resp["ts"])

    async def update(
        self, ref: ChatMessageRef, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        await self.client.chat_update(channel=ref.channel, ts=ref.ts, text=text, blocks=blocks)


class RecordingChatApp:
    """In-memory ChatApp for tests and `RELAY_ENVIRONMENT=test`. Records every call."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.dms: list[dict[str, Any]] = []
        self._n = 0

    def _ref(self, channel: str) -> ChatMessageRef:
        self._n += 1
        return ChatMessageRef(channel=channel, ts=f"{self._n}.000")

    async def post(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        attribution: str | None = None,
    ) -> ChatMessageRef:
        self.posts.append(
            {"channel": channel, "text": text, "blocks": blocks, "attribution": attribution}
        )
        return self._ref(channel)

    async def dm(
        self, user_id: str, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> ChatMessageRef:
        self.dms.append({"user_id": user_id, "text": text, "blocks": blocks})
        return self._ref(f"D{user_id}")

    async def update(
        self, ref: ChatMessageRef, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        self.posts.append(
            {"channel": ref.channel, "ts": ref.ts, "text": text, "blocks": blocks, "update": True}
        )
