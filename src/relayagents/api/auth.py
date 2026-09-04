"""Per-user tokens.

* Tokens are opaque: ``rly_<ulid-ish random>``. Only a SHA-256 (with optional pepper) is stored.
* Each token belongs to a user and acts as one actor: the human (``ada``) or one of their
  agents (``ada.hermes``, ``ada.claude-code``). Every event records that actor.
* The same verifier serves REST (``Authorization: Bearer``) and the MCP server (OAuth 2.1
  bearer, via :class:`RelayTokenVerifier`).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.auth.provider import AccessToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core.config import Settings
from relayagents.core.events import Actor, Event, TokenIssued, TokenRevoked
from relayagents.core.ids import new_id
from relayagents.core.models import ApiTokenRow, UserRow
from relayagents.core.store import EventStore
from relayagents.tools.context import Principal, Services

TOKEN_PREFIX = "rly_"
DEFAULT_SCOPES = ("tools", "events:read", "events:write", "a2a")


def hash_token(token: str, pepper: str = "") -> str:
    return hashlib.sha256((pepper + token).encode()).hexdigest()


async def mint_token(
    session: AsyncSession,
    *,
    user_id: str,
    actor: Actor,
    label: str,
    settings: Settings,
    issued_by: Actor,
    issued_via: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    ttl_days: int | None = None,
) -> tuple[str, ApiTokenRow]:
    """Mint a token and log ``token.issued``. Callers enforce *who* may mint *what*."""
    if actor.user_id != user_id:
        raise ValueError(f"actor {actor.id!r} does not belong to user {user_id!r}")
    plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = ApiTokenRow(
        id=new_id("tok"),
        user_id=user_id,
        token_hash=hash_token(plain, settings.token_pepper),
        label=label,
        scopes=list(scopes),
        actor_kind=actor.kind,
        actor_id=actor.id,
        created_at=now,
        expires_at=now + timedelta(days=ttl_days or settings.token_ttl_days),
    )
    session.add(row)
    await session.flush()
    await EventStore(session).append(
        Event.new(
            TokenIssued(
                token_id=row.id,
                user_id=user_id,
                token_actor=actor,
                label=label,
                expires_at=row.expires_at,
                issued_via=issued_via,
            ),  # type: ignore[arg-type]
            actor=issued_by,
            source="api",
            thread_id=f"tokens:{user_id}",
        )
    )
    return plain, row


async def revoke_token(
    session: AsyncSession, *, token_id: str, user_id: str, by: Actor, reason: str | None = None
) -> ApiTokenRow | None:
    row = await session.get(ApiTokenRow, token_id)
    if row is None or row.user_id != user_id:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await EventStore(session).append(
            Event.new(
                TokenRevoked(token_id=token_id, user_id=user_id, reason=reason),
                actor=by,
                source="api",
                thread_id=f"tokens:{user_id}",
            )
        )
    return row


async def lookup_token(
    session: AsyncSession, plain: str, settings: Settings
) -> tuple[ApiTokenRow, UserRow] | None:
    if not plain.startswith(TOKEN_PREFIX):
        return None
    row = await session.scalar(
        select(ApiTokenRow).where(
            ApiTokenRow.token_hash == hash_token(plain, settings.token_pepper)
        )
    )
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at and row.expires_at < datetime.now(UTC):
        return None
    user = await session.get(UserRow, row.user_id)
    if user is None:
        return None
    row.last_used_at = datetime.now(UTC)
    return row, user


def principal_from(row: ApiTokenRow, user: UserRow) -> Principal:
    return Principal(
        user_id=user.id,
        actor=Actor(kind=row.actor_kind, id=row.actor_id),  # type: ignore[arg-type]
        display_name=user.display_name,
        token_id=row.id,
        scopes=tuple(row.scopes),
        is_admin=user.is_admin,
    )


async def authenticate(services: Services, plain: str) -> Principal | None:
    async with services.db.session() as session:
        found = await lookup_token(session, plain, services.settings)
        if found is None:
            return None
        row, user = found
        await session.commit()
        return principal_from(row, user)


# ---- FastAPI dependencies --------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


def get_services(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


async def current_principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    services: Annotated[Services, Depends(get_services)],
) -> Principal:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await authenticate(services, creds.credentials)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def human_principal(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """Routes that change identity, credentials, or approvals: humans only, never their agents."""
    if principal.actor.kind != "human":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this action needs a human token, not an agent token"
        )
    return principal


async def admin_principal(principal: Annotated[Principal, Depends(human_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return principal


# ---- MCP token verifier ----------------------------------------------------------------------


class RelayTokenVerifier:
    """``mcp.server.auth.provider.TokenVerifier`` backed by the Relay token table."""

    def __init__(self, services: Services) -> None:
        self.services = services

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await authenticate(self.services, token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.user_id,
            scopes=list(principal.scopes),
            subject=principal.actor.id,
            claims={
                "user_id": principal.user_id,
                "actor_kind": principal.actor.kind,
                "actor_id": principal.actor.id,
                "display_name": principal.display_name,
                "token_id": principal.token_id,
                "is_admin": principal.is_admin,
            },
        )


def principal_from_access_token(tok: AccessToken) -> Principal:
    c = tok.claims or {}
    return Principal(
        user_id=c.get("user_id") or tok.client_id,
        actor=Actor(
            kind=c.get("actor_kind", "human"), id=c.get("actor_id") or tok.subject or tok.client_id
        ),
        display_name=c.get("display_name", ""),
        token_id=c.get("token_id"),
        scopes=tuple(tok.scopes),
        is_admin=bool(c.get("is_admin", False)),
    )
