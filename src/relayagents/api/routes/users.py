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
from relayagents.api.auth import (
    admin_principal,
    current_principal,
    get_services,
    human_principal,
    mint_token,
    revoke_token,
)
from relayagents.core.events import Actor, Event, UserUpdated
from relayagents.core.models import AgentRow, ApiTokenRow, DeviceCodeRow, UserRow
from relayagents.core.store import EventStore
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
    principal: Annotated[Principal, Depends(human_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> UserOut:
    """Identity bindings and posting mode gate approvals, so only the human may change them."""
    changes = body.model_dump(exclude_none=True)
    async with services.db.session() as session:
        user = await session.get(UserRow, principal.user_id)
        assert user is not None
        if "slack_user_id" in changes:
            clash = await session.scalar(
                select(UserRow).where(
                    UserRow.slack_user_id == changes["slack_user_id"], UserRow.id != user.id
                )
            )
            if clash is not None:
                raise HTTPException(
                    409, "that Slack user id is already bound to another Relay user"
                )
        for k, v in changes.items():
            setattr(user, k, v)
        if changes:
            await EventStore(session).append(
                Event.new(
                    UserUpdated(user_id=user.id, changes=changes),
                    actor=principal.actor,
                    source="api",
                    thread_id=f"user:{user.id}",
                )
            )
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


RESERVED_USER_IDS = {"relay", "system", "admin"}


class AddUserIn(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    display_name: str
    email: str | None = None
    slack_user_id: str | None = None
    github_login: str | None = None
    timezone: str = "UTC"
    harness: str = Field(default="hermes", pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    is_admin: bool = False
    reissue: bool = Field(
        default=False,
        description="Mint fresh tokens for an existing user (logged as token.issued).",
    )


class AddUserOut(BaseModel):
    user: UserOut
    human_token: str
    agent_id: str
    agent_token: str
    relay_url: str


async def create_user_with_tokens(
    services: Services, body: AddUserIn, *, issued_by: Actor
) -> AddUserOut:
    """Shared by the admin REST route and `relay add-user` (which runs on the node)."""
    if body.id in RESERVED_USER_IDS or body.id.startswith("relay"):
        raise HTTPException(400, f"user id {body.id!r} is reserved for system actors")
    now = datetime.now(UTC)
    async with services.db.session() as session:
        user = await session.get(UserRow, body.id)
        if user is not None and not body.reissue:
            raise HTTPException(
                409, f"user {body.id!r} already exists; pass reissue=true to mint new tokens"
            )
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
        via = "admin" if issued_by.id != user.id else "add_user"
        human_token, _ = await mint_token(
            session,
            user_id=user.id,
            actor=Actor.human(user.id),
            label="human",
            settings=services.settings,
            issued_by=issued_by,
            issued_via=via,
        )  # type: ignore[arg-type]
        agent_id = f"{user.id}.{body.harness}"
        agent_token, _ = await mint_token(
            session,
            user_id=user.id,
            actor=Actor.agent(agent_id),
            label=f"agent:{body.harness}",
            settings=services.settings,
            issued_by=issued_by,
            issued_via=via,
        )  # type: ignore[arg-type]
        card = default_agent_card(
            agent_id, user.display_name, services.settings.public_url, body.harness
        )
        await broker.register_agent(
            session,
            agent_id=agent_id,
            user_id=user.id,
            harness=body.harness,
            card=card,
            by=issued_by,
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
    admin: Annotated[Principal, Depends(admin_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> AddUserOut:
    return await create_user_with_tokens(services, body, issued_by=admin.actor)


class TokenIn(BaseModel):
    label: str = Field(default="cli", pattern=r"^[\w@.:-]{1,64}$")
    actor_kind: str = Field(default="human", pattern="^(human|agent)$")
    harness: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,31}$",
        description="For agent tokens: the harness name, e.g. claude-code.",
    )


@router.post("/tokens", status_code=201)
async def create_token(
    body: TokenIn,
    principal: Annotated[Principal, Depends(human_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    """Only a human token can mint tokens (for itself or for one of its agents). Agents cannot escalate."""
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
            issued_by=principal.actor,
            issued_via="api",
        )
        await session.commit()
    return {
        "token": plain,
        "token_id": row.id,
        "actor": actor.model_dump(),
        "expires_at": row.expires_at,
    }


@router.get("/tokens")
async def list_tokens(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> list[dict[str, Any]]:
    async with services.db.session() as session:
        rows = (
            await session.scalars(
                select(ApiTokenRow)
                .where(ApiTokenRow.user_id == principal.user_id)
                .order_by(ApiTokenRow.created_at)
            )
        ).all()
    return [
        {
            "token_id": r.id,
            "label": r.label,
            "actor": {"kind": r.actor_kind, "id": r.actor_id},
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "revoked_at": r.revoked_at,
            "last_used_at": r.last_used_at,
            "current": r.id == principal.token_id,
        }
        for r in rows
    ]


@router.delete("/tokens/{token_id}")
async def delete_token(
    token_id: str,
    principal: Annotated[Principal, Depends(human_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    async with services.db.session() as session:
        row = await revoke_token(
            session, token_id=token_id, user_id=principal.user_id, by=principal.actor
        )
        if row is None:
            raise HTTPException(404, "no such token")
        await session.commit()
    return {"token_id": row.id, "revoked_at": row.revoked_at}


# ---- device flow (relay login) ---------------------------------------------------------------


class DeviceStartIn(BaseModel):
    user_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    label: str = Field(default="cli", pattern=r"^[\w@.:-]{1,64}$")


DEVICE_COOLDOWN_S = 60


@router.post("/auth/device")
async def device_start(body: DeviceStartIn, request: Request) -> dict[str, Any]:
    services: Services = request.app.state.services
    now = datetime.now(UTC)
    async with services.db.session() as session:
        user = await session.get(UserRow, body.user_id)
        if user is None:
            raise HTTPException(404, f"unknown user {body.user_id!r}")
        recent = await session.scalar(
            select(DeviceCodeRow).where(
                DeviceCodeRow.user_id == user.id,
                DeviceCodeRow.status == "pending",
                DeviceCodeRow.created_at > now - timedelta(seconds=DEVICE_COOLDOWN_S),
            )
        )
        if recent is not None:
            raise HTTPException(
                429,
                f"a login request for {user.id!r} is already pending; try again in {DEVICE_COOLDOWN_S}s",
            )
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
                        "text": f":key: *Login request* for `{user.id}` from `{body.label}`.\nYour terminal shows code *{row.user_code}*. Approve only if the codes match and you started this.",
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
    services: Services,
    device_code: str,
    *,
    approved: bool,
    by_user: str | None = None,
    admin: Actor | None = None,
) -> DeviceCodeRow:
    """Resolve a login request. Either the account owner (``by_user``) or an admin (``admin``) may approve; nobody else."""
    async with services.db.session() as session:
        row = await session.get(DeviceCodeRow, device_code)
        if row is None:
            raise KeyError(device_code)
        if row.status != "pending":
            return row
        if admin is None and by_user != row.user_id:
            raise PermissionError(
                "only the account owner (or an admin on the node) can approve a login"
            )
        if approved:
            plain, tok = await mint_token(
                session,
                user_id=row.user_id,
                actor=Actor.human(row.user_id),
                label=row.label,
                settings=services.settings,
                issued_by=admin or Actor.human(row.user_id),
                issued_via="admin" if admin else "device_flow",
            )
            row.status, row.token_id, row.token_plain = "approved", tok.id, plain
        else:
            row.status = "denied"
        await session.commit()
        return row


@router.post("/auth/device/{device_code}/approve")
async def device_approve_admin(
    device_code: str,
    admin: Annotated[Principal, Depends(admin_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, str]:
    """Fallback when Slack is not configured: an admin approves from the node."""
    try:
        row = await approve_device(services, device_code, approved=True, admin=admin.actor)
    except KeyError as exc:
        raise HTTPException(404, "unknown device code") from exc
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
