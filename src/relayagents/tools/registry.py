"""The single definition of Relay's tool surface."""

from __future__ import annotations

from pydantic import BaseModel

from relayagents.tools import handlers, schemas
from relayagents.tools.spec import ToolSpec


def _render_items(out: BaseModel) -> str:
    items = out.items  # type: ignore[attr-defined]
    if not items:
        return "no items"
    return "\n".join(
        f"{i.id}  [{i.status:<11}] {i.title}" + (f"  → {i.assignee}" if i.assignee else "")
        for i in items
    )


def _render_events(out: BaseModel) -> str:
    evs = out.events  # type: ignore[attr-defined]
    if not evs:
        return "no events"
    lines = []
    for e in evs:
        p = e.payload.model_dump()
        text = next(
            (str(p[k]) for k in ("statement", "text", "title", "action", "answer") if p.get(k)), ""
        )
        lines.append(f"{e.ts:%Y-%m-%d %H:%M}  {e.id}  {e.type:<22} {e.actor.id:<18} {text[:80]}")
    return "\n".join(lines)


def _render_recall(out: BaseModel) -> str:
    hits = out.hits  # type: ignore[attr-defined]
    if not hits:
        return "nothing found"
    return "\n".join(
        f"{h.score:.2f} {h.kind:<6} {h.text[:100]}  ({', '.join(h.event_ids) or h.ref or ''})"
        for h in hits
    )


def _render_decisions(out: BaseModel) -> str:
    ds = out.decisions  # type: ignore[attr-defined]
    if not ds:
        return "no decisions"
    return "\n".join(
        f"{d.decided_at:%Y-%m-%d}  {d.id}  {d.statement}"
        + (f"  [superseded by {d.superseded_by}]" if d.superseded_by else "")
        for d in ds
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="recall",
        description="Hybrid search over team memory (graph + vectors + event log). Returns hits with provenance (event ids) so you can cite them.",
        input_model=schemas.RecallInput,
        output_model=schemas.RecallOutput,
        handler=handlers.recall,
        read_only=True,
        positional=("query",),
        render=_render_recall,
    ),
    ToolSpec(
        name="my_items",
        description="Your open action items, with the source meeting and current status.",
        input_model=schemas.MyItemsInput,
        output_model=schemas.ItemsOutput,
        handler=handlers.my_items,
        read_only=True,
        render=_render_items,
    ),
    ToolSpec(
        name="items",
        description="Action items for a teammate (--assignee) or the whole team.",
        input_model=schemas.ItemsInput,
        output_model=schemas.ItemsOutput,
        handler=handlers.items,
        read_only=True,
        render=_render_items,
    ),
    ToolSpec(
        name="events",
        description="Query the event log: by time window (--since 24h), type, thread, or actor ('me').",
        input_model=schemas.EventsInput,
        output_model=schemas.EventsOutput,
        handler=handlers.events,
        read_only=True,
        render=_render_events,
    ),
    ToolSpec(
        name="report",
        description="Publish what you did as a report.posted event. Source of truth for standups and item closure. Optionally close the item.",
        input_model=schemas.ReportInput,
        output_model=schemas.ReportOutput,
        handler=handlers.report,
        read_only=False,
        action_type="relay.report",
        positional=("text",),
        render=lambda o: (
            f"reported {o.event_id}" + (f", closed {o.closed_item_id}" if o.closed_item_id else "")
        ),  # type: ignore[attr-defined]
    ),
    ToolSpec(
        name="ask",
        description="Ask a teammate's agent a question over A2A (through Relay's broker). The teammate is notified in Slack. Threaded.",
        input_model=schemas.AskInput,
        output_model=schemas.AskOutput,
        handler=handlers.ask,
        read_only=False,
        action_type="relay.ask",
        positional=("user", "question"),
        render=lambda o: (
            f"task {o.task_id} → {o.to_agent} [{o.state}] thread {o.thread_id}"
            + (f"\n{o.answer}" if o.answer else "")
        ),  # type: ignore[attr-defined]
    ),
    ToolSpec(
        name="request_approval",
        description="Open an approval for an external action. The human resolves it in Slack; blocks until then (or timeout).",
        input_model=schemas.RequestApprovalInput,
        output_model=schemas.RequestApprovalOutput,
        handler=handlers.request_approval,
        read_only=False,
        action_type="generic",
        positional=("action",),
        render=lambda o: (
            f"{o.approval_id}: {o.status}"
            + (f" (edited: {o.edited_action})" if o.edited_action else "")
        ),  # type: ignore[attr-defined]
    ),
    ToolSpec(
        name="decisions",
        description="Decisions with dates and superseded-by links, optionally filtered by topic.",
        input_model=schemas.DecisionsInput,
        output_model=schemas.DecisionsOutput,
        handler=handlers.decisions,
        read_only=True,
        render=_render_decisions,
    ),
    ToolSpec(
        name="post",
        description="Post to Slack through Relay's app with 'posted by X's agent' attribution.",
        input_model=schemas.PostInput,
        output_model=schemas.PostOutput,
        handler=handlers.post,
        read_only=False,
        action_type="slack.post.as_agent",
        positional=("text",),
        render=lambda o: f"posted {o.channel}/{o.ts} ({o.event_id})",  # type: ignore[attr-defined]
    ),
)

_BY_NAME = {t.name: t for t in TOOLS}


def get_tool(name: str) -> ToolSpec:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown tool {name!r}; known: {', '.join(_BY_NAME)}") from exc
