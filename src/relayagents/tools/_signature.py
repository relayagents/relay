"""Build a Python function whose signature mirrors a Pydantic input model.

Both the MCP SDK and Typer introspect ``inspect.signature`` to derive schemas/options, so
generating a signature from the model is how we keep the three transports in lockstep.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, Field
from pydantic_core import PydanticUndefined


def field_default(model: type[BaseModel], name: str) -> Any:
    f = model.model_fields[name]
    if f.default is not PydanticUndefined:
        return f.default
    if f.default_factory is not None:
        return f.default_factory()  # type: ignore[call-arg]
    return inspect.Parameter.empty


def mirror_signature(
    model: type[BaseModel],
    impl: Callable[..., Any],
    *,
    extra_params: list[inspect.Parameter] | None = None,
    return_annotation: Any = inspect.Signature.empty,
) -> Callable[..., Any]:
    """Return ``impl`` with ``__signature__``/``__annotations__`` derived from ``model``."""
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        default = field_default(model, name)
        ann = (
            Annotated[f.annotation, Field(description=f.description)]
            if f.description
            else f.annotation
        )
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann)
        )
        annotations[name] = ann
    for p in extra_params or []:
        params.append(p)
        annotations[p.name] = p.annotation
    # Required params must precede optional ones for a valid signature (keyword-only allows any order,
    # but keep it tidy for CLI help).
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    impl.__signature__ = inspect.Signature(params, return_annotation=return_annotation)  # type: ignore[attr-defined]
    if return_annotation is not inspect.Signature.empty:
        annotations["return"] = return_annotation
    impl.__annotations__ = annotations
    return impl
