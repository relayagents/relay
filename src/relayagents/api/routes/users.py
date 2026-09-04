"""Users, tokens, and the `relay login` device flow."""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from relayagents.api.a2a_broker import broker
from relayagents.api.a2a_broker.types import default_agent_card
from relayagents.api.auth import admin_principal, current_principal, get_services, mint_token
from relayagents.core.events import Actor
from relayagents.core.models import AgentRow, DeviceCodeRow, UserRow
from relayagents.tools.context import Principal, Services

router = APIRouter(prefix="/v1", tags=["users"])


class UserOut(BaseModel):
    id: str
    display_name: str
    email: str | None
    slack_user_id: str | None
    github_login: str | None
    timezone: str
    standup_mode: str
    standup_time: str
    is_admin: bool


def _user_out(u: UserRow) -> UserOut:
    return UserOut(
        id=u.id,
        display_name=u.display_name,
        email=u.email,
        slack_user_id=u.slack_user_id,
        github_login=u.github_login,
        timezone=u.timezone,
        standup_mode=u.standup_mode,
        standup_time=u.standup_time,
        is_admin=u.is_admin,
    )


@router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    async with services.db.session() as session:
        user = await session.get(UserRow, principal.user_id)
        agents = (
            await session.scalars(select(AgentRow).where(AgentRow.user_id == principal.user_id))
        ).all()
    assert user is not None
    return {
        "user": _user_out(user).model_dump(),
        "actor": principal.actor.model_dump(),
        "token_id": principal.token_id,
        "scopes": list(principal.scopes),
        "agents": [a.id for a in agents],
    }


class UserSettingsIn(BaseModel):
    standup_mode: str | None = Field(default=None, pattern="^(draft|auto|off)$")
    standup_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = None
    slack_user_id: str | None = None
    github_login: str | None = None


@router.patch("/me")
async def update_me(
    body: UserSettingsIn,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> UserOut:
    async with services.db.session() as session:
        user = await session.get(UserRow, principal.user_id)
        assert user is not None
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(user, k, v)
        await session.commit()
        return _user_out(user)


@router.get("/users")
async def list_users(
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> list[UserOut]:
    async with services.db.session() as session:
        return [
            _user_out(u)
            for u in (await session.scalars(select(UserRow).order_by(UserRow.id))).all()
        ]


class AddUserIn(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    display_name: str
    email: str | None = None
    slack_user_id: str | None = None
    github_login: str | None = None
    timezone: str = "UTC"
    harness: str = "hermes"
    is_admin: bool = False


class AddUserOut(BaseModel):
    user: UserOut
    human_token: str
    agent_id: str
    agent_token: str
    relay_url: str


async def create_user_with_tokens(services: Services, body: AddUserIn) -> AddUserOut:
    """Shared by the admin REST route and `relay add-user` (which runs on the node)."""
    now = datetime.now(UTC)
    async with services.db.session() as session:
        user = await session.get(UserRow, body.id)
        if user is None:
            user = UserRow(
                id=body.id,
                display_name=body.display_name,
                email=body.email,
                slack_user_id=body.slack_user_id,
                github_login=body.github_login,
                timezone=body.timezone,
                is_admin=body.is_admin,
                created_at=now,
            )
            session.add(user)
            await session.flush()
        human_token, _ = await mint_token(
            session,
            user_id=user.id,
            actor=Actor.human(user.id),
            label="human",
            settings=services.settings,
        )
        agent_id = f"{user.id}.{body.harness}"
        agent_token, _ = await mint_token(
            session,
            user_id=user.id,
            actor=Actor.agent(agent_id),
            label=f"agent:{body.harness}",
            settings=services.settings,
        )
        card = default_agent_card(
            agent_id, user.display_name, services.settings.public_url, body.harness
        )
        await broker.register_agent(
            session, agent_id=agent_id, user_id=user.id, harness=body.harness, card=card
        )
        await session.commit()
        return AddUserOut(
            user=_user_out(user),
            human_token=human_token,
            agent_id=agent_id,
            agent_token=agent_token,
            relay_url=services.settings.public_url,
        )


@router.post("/users", status_code=201)
async def add_user(
    body: AddUserIn,
    _: Annotated[Principal, Depends(admin_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> AddUserOut:
    return await create_user_with_tokens(services, body)


class TokenIn(BaseModel):
    label: str = "cli"
    actor_kind: str = Field(default="human", pattern="^(human|agent)$")
    harness: str | None = Field(
        default=None, description="For agent tokens: the harness name, e.g. claude-code."
    )


@router.post("/tokens", status_code=201)
async def create_token(
    body: TokenIn,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    actor = (
        Actor.human(principal.user_id)
        if body.actor_kind == "human"
        else Actor.agent(f"{principal.user_id}.{body.harness or 'agent'}")
    )
    async with services.db.session() as session:
        plain, row = await mint_token(
            session,
            user_id=principal.user_id,
            actor=actor,
            label=body.label,
            settings=services.settings,
        )
        await session.commit()
    return {
        "token": plain,
        "token_id": row.id,
        "actor": actor.model_dump(),
        "expires_at": row.expires_at,
    }


# ---- device flow (relay login) ---------------------------------------------------------------


class DeviceStartIn(BaseModel):
    user_id: str
    label: str = "cli"


@router.post("/auth/device")
async def device_start(body: DeviceStartIn, request: Request) -> dict[str, Any]:
    services: Services = request.app.state.services
    now = datetime.now(UTC)
    async with services.db.session() as session:
        user = await session.get(UserRow, body.user_id)
        if user is None:
            raise HTTPException(404, f"unknown user {body.user_id!r}")
        row = DeviceCodeRow(
            device_code=secrets.token_urlsafe(32),
            user_code=secrets.token_hex(3).upper(),
            user_id=user.id,
            label=body.label,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        session.add(row)
        await session.commit()
        if services.chat is not None:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":key: *Login request* from `{body.label}` for `{user.id}`. Code *{row.user_code}*. Approve only if this is you.",
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"login:{row.device_code}",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "Approve login"},
                            "action_id": "login_approve",
                            "value": row.device_code,
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "action_id": "login_deny",
                            "value": row.device_code,
                        },
                    ],
                },
            ]
            with contextlib.suppress(Exception):
                await services.chat.dm(
                    user.id, f"Login request {row.user_code} from {body.label}", blocks=blocks
                )
        return {
            "device_code": row.device_code,
            "user_code": row.user_code,
            "expires_in": 600,
            "interval": 3,
            "slack": services.chat is not None,
        }


async def approve_device(
    services: Services, device_code: str, *, approved: bool, by_user: str | None = None
) -> DeviceCodeRow:
    async with services.db.session() as session:
        row = await session.get(DeviceCodeRow, device_code)
        if row is None:
            raise KeyError(device_code)
        if row.status != "pending":
            return row
        if by_user is not None and by_user != row.user_id:
            raise PermissionError("only the account owner can approve a login")
        if approved:
            plain, tok = await mint_token(
                session,
                user_id=row.user_id,
                actor=Actor.human(row.user_id),
                label=row.label,
                settings=services.settings,
            )
            row.status, row.token_id, row.token_plain = "approved", tok.id, plain
        else:
            row.status = "denied"
        await session.commit()
        return row


@router.post("/auth/device/{device_code}/approve")
async def device_approve_admin(
    device_code: str,
    _: Annotated[Principal, Depends(admin_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, str]:
    """Fallback when Slack is not configured: an admin approves from the node."""
    row = await approve_device(services, device_code, approved=True)
    return {"status": row.status}


@router.get("/auth/device/{device_code}")
async def device_poll(device_code: str, request: Request) -> dict[str, Any]:
    services: Services = request.app.state.services
    async with services.db.session() as session:
        row = await session.get(DeviceCodeRow, device_code)
        if row is None:
            raise HTTPException(404, "unknown device code")
        if row.status == "pending" and datetime.now(UTC) > row.expires_at:
            row.status = "expired"
        out: dict[str, Any] = {"status": row.status, "user_id": row.user_id}
        if row.status == "approved" and row.token_plain:
            out["token"] = row.token_plain
            row.token_plain = None  # one-shot
        await session.commit()
        return out
