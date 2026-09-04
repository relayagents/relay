"""Generate `relay <tool>` subcommands from the registry. Each command POSTs to REST."""

from __future__ import annotations

import inspect
import json
import types
from typing import Annotated, Any, Literal, get_args, get_origin

import typer
from pydantic import BaseModel

from relayagents.tools._signature import field_default
from relayagents.tools.registry import TOOLS
from relayagents.tools.spec import ToolSpec


def _unwrap_optional(ann: Any) -> tuple[Any, bool]:
    if get_origin(ann) in (types.UnionType,) or str(get_origin(ann)) == "typing.Union":
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return ann, False


def _cli_type(ann: Any) -> Any:
    """Map a model field annotation to something Typer/Click understands."""
    base, _ = _unwrap_optional(ann)
    if get_origin(base) is Literal:
        return str  # validated server-side; help text lists choices
    if get_origin(base) is list:
        inner = get_args(base)[0]
        inner_base, _ = _unwrap_optional(inner)
        return (
            list[str]
            if get_origin(inner_base) is Literal or inner_base is str
            else list[inner_base]
        )  # type: ignore[valid-type]
    if get_origin(base) is dict:
        return str  # JSON string
    return base


def _help(model: type[BaseModel], name: str) -> str:
    f = model.model_fields[name]
    text = f.description or ""
    base, _ = _unwrap_optional(f.annotation)
    if get_origin(base) is Literal:
        text += f" One of: {', '.join(map(str, get_args(base)))}."
    if get_origin(base) is dict:
        text += " (JSON object)"
    return text.strip()


def _coerce(model: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, v in values.items():
        if v is None or v == () or v == []:
            continue
        f = model.model_fields[name]
        base, _ = _unwrap_optional(f.annotation)
        if get_origin(base) is dict and isinstance(v, str):
            v = json.loads(v)
        if isinstance(v, tuple):
            v = list(v)
        out[name] = v
    return out


def make_command(spec: ToolSpec, client_factory: Any) -> Any:
    model = spec.input_model

    def impl(**kwargs: Any) -> None:
        as_json = kwargs.pop("json_output", False)
        args = _coerce(model, kwargs)
        client = client_factory()
        try:
            result = client.call_tool(spec.name, args)
        except Exception as exc:
            typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if as_json or spec.render is None:
            typer.echo(json.dumps(result, indent=2, default=str))
        else:
            typer.echo(spec.render(spec.output_model.model_validate(result)))

    params: list[inspect.Parameter] = []
    for name in model.model_fields:
        default = field_default(model, name)
        required = default is inspect.Parameter.empty
        ctype = _cli_type(model.model_fields[name].annotation)
        help_text = _help(model, name)
        if name in spec.positional:
            p_default = (
                typer.Argument(..., help=help_text)
                if required
                else typer.Argument(default, help=help_text)
            )
        else:
            p_default = typer.Option(
                ... if required else default, f"--{name.replace('_', '-')}", help=help_text
            )
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=p_default, annotation=ctype
            )
        )
    params.append(
        inspect.Parameter(
            "json_output",
            inspect.Parameter.KEYWORD_ONLY,
            default=typer.Option(False, "--json", help="Print raw JSON."),
            annotation=bool,
        )
    )
    params.sort(key=lambda p: not (isinstance(p.default, typer.models.ArgumentInfo)))
    impl.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    impl.__annotations__ = {p.name: p.annotation for p in params}
    impl.__name__ = spec.name
    impl.__doc__ = spec.description
    return impl


def register_tool_commands(app: typer.Typer, client_factory: Any) -> None:
    for spec in TOOLS:
        app.command(name=spec.name.replace("_", "-"), help=spec.description)(
            make_command(spec, client_factory)
        )


__all__ = ["Annotated", "make_command", "register_tool_commands"]
