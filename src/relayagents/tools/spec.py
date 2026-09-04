from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from relayagents.tools.context import ToolContext

Handler = Callable[[ToolContext, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler
    read_only: bool = True
    action_type: str | None = field(
        default=None
    )  # policy key when the tool touches an external system
    positional: tuple[str, ...] = ()  # CLI: fields taken as positional arguments, in order
    render: Callable[[BaseModel], str] | None = None  # CLI: human-readable output

    @property
    def title(self) -> str:
        return self.name.replace("_", " ")
