"""Generate the Relay MCP server from the registry."""

from __future__ import annotations

from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from relayagents.api.auth import RelayTokenVerifier, principal_from_access_token
from relayagents.tools._signature import mirror_signature
from relayagents.tools.context import Principal, Services, ToolContext
from relayagents.tools.handlers import ToolError
from relayagents.tools.registry import TOOLS
from relayagents.tools.runtime import invoke
from relayagents.tools.spec import ToolSpec

INSTRUCTIONS = (
    "Relay is your team's shared memory and switchboard. Use `recall` before answering questions about "
    "past decisions or work; `my_items` for what is assigned to you; `report` whenever you finish "
    "something (this is how standups get written); `ask` to reach a teammate's agent; `request_approval` "
    "before any external write. Everything you do here is visible to your team."
)


def _principal() -> Principal:
    # The MCP mount always runs behind RequireAuthMiddleware, so a missing token cannot reach here.
    tok = get_access_token()
    if tok is None:
        raise ToolError("unauthenticated")
    return principal_from_access_token(tok)


def _make_tool(spec: ToolSpec, services: Services) -> Any:
    async def impl(**kwargs: Any) -> Any:
        ctx = ToolContext(principal=_principal(), services=services, transport="mcp")
        return await invoke(spec, ctx, kwargs)

    impl.__name__ = spec.name
    impl.__doc__ = spec.description
    return mirror_signature(spec.input_model, impl, return_annotation=spec.output_model)


def build_mcp_server(services: Services) -> MCPServer:
    public = services.settings.public_url.rstrip("/")
    kwargs: dict[str, Any] = {
        "token_verifier": RelayTokenVerifier(services),
        "auth": AuthSettings(
            issuer_url=public, resource_server_url=f"{public}/mcp", required_scopes=["tools"]
        ),
    }
    server = MCPServer(
        name="relay", instructions=INSTRUCTIONS, website_url="https://relayagents.dev", **kwargs
    )
    for spec in TOOLS:
        server.add_tool(
            _make_tool(spec, services),
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=ToolAnnotations(
                read_only_hint=spec.read_only,
                destructive_hint=False,
                open_world_hint=spec.action_type not in (None, "relay.report", "relay.ask"),
            ),
        )
    return server
