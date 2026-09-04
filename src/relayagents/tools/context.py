"""What a tool handler gets: who is calling, over what transport, and the services it may use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from relayagents.core.config import Settings
from relayagents.core.db import Database
from relayagents.core.events import Actor
from relayagents.core.protocols import ChatApp, Embedder, MemoryStore, OfficeSuite, SemanticSearch

Transport = Literal["mcp", "cli", "rest", "internal"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    actor: Actor
    display_name: str = ""
    token_id: str | None = None
    scopes: tuple[str, ...] = ()
    is_admin: bool = False


@dataclass
class Services:
    db: Database
    settings: Settings
    chat: ChatApp | None = None
    office: OfficeSuite | None = None
    # Workers only: hold the team model key.
    memory: MemoryStore | None = None
    embedder: Embedder | None = None
    # API side: the job queue (arq pool) and the semantic legs reached through it.
    queue: Any | None = None
    semantic: SemanticSearch | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    principal: Principal
    services: Services
    transport: Transport = "internal"

    @property
    def db(self) -> Database:
        return self.services.db

    @property
    def settings(self) -> Settings:
        return self.services.settings

    @property
    def actor(self) -> Actor:
        return self.principal.actor

    @property
    def user_id(self) -> str:
        return self.principal.user_id

    def resolve_user(self, value: str | None) -> str | None:
        if value is None:
            return None
        v = value.lstrip("@")
        return self.user_id if v == "me" else v
