"""Generate the REST tool endpoints (``POST /v1/tools/<name>``) from the registry."""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from relayagents.api.auth import current_principal
from relayagents.tools.context import Principal, ToolContext
from relayagents.tools.handlers import ToolError
from relayagents.tools.registry import TOOLS
from relayagents.tools.runtime import invoke
from relayagents.tools.spec import ToolSpec

TOOLS_PREFIX = "/v1/tools"


def _endpoint(spec: ToolSpec) -> Any:
    async def endpoint(request: Request, body: Any, principal: Principal) -> Any:
        ctx = ToolContext(
            principal=principal, services=request.app.state.services, transport="rest"
        )
        try:
            return await invoke(spec, ctx, body)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc

    endpoint.__name__ = f"tool_{spec.name}"
    endpoint.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request
            ),
            inspect.Parameter(
                "body", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=spec.input_model
            ),
            inspect.Parameter(
                "principal",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Principal,
                default=Depends(current_principal),
            ),
        ]
    )
    return endpoint


def build_router() -> APIRouter:
    router = APIRouter(prefix=TOOLS_PREFIX, tags=["tools"])
    for spec in TOOLS:
        router.add_api_route(
            f"/{spec.name}",
            _endpoint(spec),
            methods=["POST"],
            response_model=spec.output_model,
            name=spec.name,
            summary=spec.description,
            operation_id=f"tool_{spec.name}",
        )

    @router.get("", summary="List the tool surface with input/output schemas.")
    async def list_tools() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "read_only": t.read_only,
                    "action_type": t.action_type,
                    "input_schema": t.input_model.model_json_schema(),
                    "output_schema": t.output_model.model_json_schema(),
                }
                for t in TOOLS
            ]
        }

    return router
