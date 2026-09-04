"""relay-api application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from relayagents import __version__
from relayagents.api.a2a_broker.routes import router as a2a_router
from relayagents.api.routes.approvals import router as approvals_router
from relayagents.api.routes.events import router as events_router
from relayagents.api.routes.health import router as health_router
from relayagents.api.routes.meetings import router as meetings_router
from relayagents.api.routes.standups import router as standups_router
from relayagents.api.routes.users import router as users_router
from relayagents.core.config import Settings, get_settings
from relayagents.core.db import Database
from relayagents.tools.context import Services
from relayagents.tools.handlers import ToolError
from relayagents.tools.mcp import build_mcp_server
from relayagents.tools.rest import build_router as build_tools_router

log = structlog.get_logger()


def build_services(
    settings: Settings, *, db: Database | None = None, role: str = "api"
) -> Services:
    """``role="api"`` never touches a model provider; ``role="worker"`` holds the team key."""
    db = db or Database(settings.database_url)
    services = Services(db=db, settings=settings)
    if settings.slack_enabled:
        from relayagents.connectors.slack import SlackChatApp

        # Buttons only work when the API holds the Socket Mode connection (app token).
        services.chat = SlackChatApp(
            settings.slack_bot_token, db, supports_actions=settings.slack_socket_mode_enabled
        )
    if settings.workspace_mcp_url:
        from relayagents.connectors.workspace import WorkspaceMCP

        services.office = WorkspaceMCP(settings.workspace_mcp_url)
    if role == "worker":
        from relayagents.connectors.memory.embeddings import make_embedder

        services.embedder = make_embedder(settings)
        if settings.memory_backend == "graphiti-kuzu":
            import importlib.util

            if importlib.util.find_spec("graphiti_core") is None:
                log.warning(
                    "memory.unavailable",
                    reason="graphiti_core is not installed; build the image with RELAY_EXTRAS='--extra graph'",
                )
            else:
                from relayagents.connectors.memory import GraphitiKuzuMemory

                services.memory = GraphitiKuzuMemory(settings)
    return services


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    services = services or build_services(settings)
    mcp = build_mcp_server(services)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.is_sqlite:
            await services.db.create_all()
        redis = None
        if settings.environment != "test":
            try:
                from relayagents.core.queue import connect

                redis = await connect(settings.redis_url)
            except Exception as exc:
                log.warning("redis.unavailable", error=str(exc))
        app.state.redis = redis
        services.queue = redis
        if redis is not None and services.semantic is None:
            from relayagents.connectors.memory.search import ArqSemanticSearch

            services.semantic = ArqSemanticSearch(redis)
        slack = None
        if settings.slack_socket_mode_enabled and settings.environment != "test":
            from relayagents.api.slack.app import SlackRunner

            slack = SlackRunner(services)
            try:
                await slack.start()
            except Exception as exc:
                log.warning("slack.start_failed", error=str(exc))
                slack = None
        async with mcp.session_manager.run():
            yield
        if slack is not None:
            await slack.stop()
        if redis is not None:
            await redis.aclose()
        if services.memory is not None:
            with contextlib.suppress(Exception):
                await services.memory.close()
        await services.db.dispose()

    app = FastAPI(
        title="Relay",
        version=__version__,
        lifespan=lifespan,
        description="Team memory and agent switchboard. See https://relayagents.dev",
    )
    app.state.services = services
    app.state.mcp = mcp
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(events_router)
    app.include_router(meetings_router)
    app.include_router(approvals_router)
    app.include_router(standups_router)
    app.include_router(a2a_router)
    app.include_router(build_tools_router())

    @app.exception_handler(ToolError)
    async def _tool_error(_: Any, exc: ToolError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # Mounted last, at the root: the MCP app owns /mcp and the /.well-known/oauth-* discovery routes.
    # DNS-rebinding protection is off because Caddy terminates TLS and only routes the configured
    # hostname; the MCP server itself is never exposed directly.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app.mount(
        "/",
        mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=security,
        ),
    )
    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "relayagents.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )
