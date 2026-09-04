"""The Relay tool surface: one definition, exposed as MCP tools, `relay` CLI subcommands, and REST.

``registry.TOOLS`` is the single source of truth. ``mcp.py``, ``rest.py`` and ``cli.py`` are
generators over it and must never carry tool-specific logic.
"""

from relayagents.tools.registry import TOOLS, get_tool
from relayagents.tools.spec import ToolSpec

__all__ = ["TOOLS", "ToolSpec", "get_tool"]
