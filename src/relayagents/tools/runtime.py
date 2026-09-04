"""``invoke``: the one code path every transport uses to run a tool.

It validates input, records ``tool.called`` / ``tool.result`` audit events for
state-changing tools, and maps errors uniformly. Read-only tools are not audited by
default to keep the log readable; set ``RELAY_AUDIT_READ_TOOLS=1`` to log them too.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from relayagents.core.events import Event, Provenance, ToolCalled, ToolResult
from relayagents.core.ids import new_id
from relayagents.core.store import EventStore
from relayagents.tools.context import ToolContext
from relayagents.tools.handlers import ToolError
from relayagents.tools.spec import ToolSpec


def _audit_reads() -> bool:
    return os.environ.get("RELAY_AUDIT_READ_TOOLS", "0") in ("1", "true", "yes")


async def invoke(
    spec: ToolSpec, ctx: ToolContext, arguments: dict[str, Any] | BaseModel
) -> BaseModel:
    try:
        inp = (
            arguments
            if isinstance(arguments, spec.input_model)
            else spec.input_model.model_validate(arguments)
        )
    except ValidationError as exc:
        raise ToolError(f"invalid arguments for {spec.name}: {exc}") from exc

    audit = (not spec.read_only) or _audit_reads()
    call_id = new_id("call")
    called_id: str | None = None
    if audit and spec.name != "post":  # post writes its own richer audit pair with the Slack target
        async with ctx.db.session() as session:
            ev = Event.new(
                ToolCalled(
                    call_id=call_id,
                    tool=spec.name,
                    transport=ctx.transport,
                    arguments=_redact(inp.model_dump(mode="json")),
                ),
                actor=ctx.actor,
                source="api" if ctx.transport in ("mcp", "rest", "internal") else "cli",
            )
            await EventStore(session).append(ev)
            await session.commit()
            called_id = ev.id

    t0 = time.perf_counter()
    ok, error = True, None
    try:
        return await spec.handler(ctx, inp)
    except ToolError as exc:
        ok, error = False, str(exc)
        raise
    except Exception as exc:
        ok, error = False, f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if called_id is not None:
            ms = int((time.perf_counter() - t0) * 1000)
            async with ctx.db.session() as session:
                await EventStore(session).append(
                    Event.new(
                        ToolResult(
                            call_id=call_id, tool=spec.name, ok=ok, error=error, duration_ms=ms
                        ),
                        actor=ctx.actor,
                        source="api" if ctx.transport in ("mcp", "rest", "internal") else "cli",
                        provenance=Provenance(
                            parent_event_ids=[called_id], tool_call_ids=[call_id]
                        ),
                    )
                )
                await session.commit()


_SENSITIVE = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+\S+|rly_[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abp]-[A-Za-z0-9-]{10,}|gh[pous]_[A-Za-z0-9]{20,})"
)


def redact(value: Any) -> Any:
    """Recursively mask secret-looking keys and token-looking strings before anything is logged."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in _SENSITIVE) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("***", value)
    return value


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    return redact(args)
