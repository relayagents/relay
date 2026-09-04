from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("RELAY_ENVIRONMENT", "test")

from relayagents.api.app import create_app
from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
from relayagents.connectors.slack.chat import RecordingChatApp
from relayagents.core.config import Settings
from relayagents.core.db import Database
from relayagents.tools.context import Services

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/relay.db",
        environment="test",
        public_url="http://testserver",
        data_dir=tmp_path / "data",
        memory_backend="none",
        extraction_model="keyword",
        slack_team_channel="C_TEAM",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def services(settings: Settings) -> AsyncIterator[Services]:
    db = Database(settings.database_url)
    await db.create_all()
    svc = Services(db=db, settings=settings, chat=RecordingChatApp())
    yield svc
    await db.dispose()


@pytest.fixture
def app(settings: Settings, services: Services):  # type: ignore[no-untyped-def]
    # No lifespan here: the MCP session manager's task group must be entered and exited in the
    # same task, so MCP tests wrap themselves in ``app.state.mcp.session_manager.run()``.
    return create_app(settings, services=services)


@pytest.fixture
async def team(services: Services) -> dict[str, dict[str, str]]:
    """Three users with human + agent tokens; ada is admin."""
    out = {}
    for uid, admin in (("ada", True), ("grace", False), ("linus", False)):
        r = await create_user_with_tokens(
            services,
            AddUserIn(
                id=uid, display_name=uid.title(), is_admin=admin, slack_user_id=f"U{uid.upper()}"
            ),
        )
        out[uid] = {"human": r.human_token, "agent": r.agent_token, "agent_id": r.agent_id}
    return out


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
