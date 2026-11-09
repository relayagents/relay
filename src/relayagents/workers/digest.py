"""Team digest: one post after the standup window closes. Quiet days say "no update"."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from relayagents.core.events import (
    ActionItemClosed,
    ActionItemUpdated,
    Actor,
    DecisionMade,
    DigestPosted,
    Event,
    Provenance,
    QuestionOpened,
    ReportPosted,
    StandupPosted,
)
from relayagents.core.indexing import index_later
from relayagents.core.store import EventStore
from relayagents.tools.context import Services

DIGEST_ACTOR = Actor.system("relay.digest")


def build_digest(
    events: list[Event], *, window_start: datetime, window_end: datetime
) -> DigestPosted:
    shipped, in_progress, blockers, needed, cited = [], [], [], [], []
    # `report --close-item` writes a report and a "done" closure by the same person: the closure
    # says it under Shipped, so the report is not also in progress. Only that person's own done
    # closure counts, so nobody can hide a teammate's report by closing their item.
    shipped_by = {
        (e.payload.item_id, e.actor.user_id)
        for e in events
        if isinstance(e.payload, ActionItemClosed) and e.payload.resolution == "done"
    }
    shown: set[str] = set()  # events that produced a line of their own
    standups: list[StandupPosted] = []
    for e in events:
        p = e.payload
        if isinstance(p, ActionItemClosed) and p.resolution == "done":
            shipped.append(f"{p.note or p.item_id} ({e.actor.user_id})")
            shown.add(e.id)
        elif isinstance(p, ReportPosted):
            if (p.item_id, e.actor.user_id) not in shipped_by:
                in_progress.append(f"{p.text} ({e.actor.user_id})")
                shown.add(e.id)
        elif isinstance(p, ActionItemUpdated) and p.status == "blocked":
            blockers.append(f"{p.note or p.item_id}")
            shown.add(e.id)
        elif isinstance(p, StandupPosted):
            standups.append(p)
        elif isinstance(p, QuestionOpened) and not p.asked_of:
            needed.append(p.text)
            shown.add(e.id)
        elif not isinstance(p, DecisionMade):
            continue
        cited.append(e.id)

    def from_standup(p: StandupPosted, lines: list[str]) -> list[str]:
        # A standup repeats the events it cites as "text [evt_...]". Skip a line only when the
        # event it cites is one of the standup's own citations and already has its own line here.
        return [
            x for x in lines if not (_cited_id(x) in p.cited_event_ids and _cited_id(x) in shown)
        ]

    for p in standups:
        shipped += [f"{d} ({p.user_id})" for d in from_standup(p, p.done)]
        in_progress += [f"{d} ({p.user_id})" for d in from_standup(p, p.doing)]
        blockers += from_standup(p, p.blocked)
    quiet = not (shipped or in_progress or blockers or needed)
    return DigestPosted(
        window_start=window_start,
        window_end=window_end,
        shipped=_dedupe(shipped),
        in_progress=_dedupe(in_progress),
        blockers=_dedupe(blockers),
        decisions_needed=_dedupe(needed),
        cited_event_ids=cited,
        quiet=quiet,
    )


_CITED = re.compile(r"\[(evt_[A-Za-z0-9]+)\]\s*$")


def _cited_id(line: str) -> str | None:
    m = _CITED.search(line)
    return m.group(1) if m else None


def _dedupe(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def render_digest(d: DigestPosted) -> str:
    if d.quiet:
        return f"*Team digest {d.window_end:%Y-%m-%d}*: no update."
    lines = [f"*Team digest {d.window_end:%Y-%m-%d}*"]
    for title, xs in (
        ("Shipped", d.shipped),
        ("In progress", d.in_progress),
        ("Blockers", d.blockers),
        ("Decisions needed today", d.decisions_needed),
    ):
        if xs:
            lines.append(f"*{title}*")
            lines += [f"• {x}" for x in xs]
    lines.append(f"_{len(d.cited_event_ids)} events cited_")
    return "\n".join(lines)


async def post_digest(services: Services, *, hours: int = 24, now: datetime | None = None) -> Event:
    now = now or datetime.now(UTC)
    start = now - timedelta(hours=hours)
    async with services.db.session() as session:
        store = EventStore(session)
        events = await store.query(since=start, until=now, limit=5000)
        events = [e for e in events if not e.type.startswith("tool.") and e.type != "digest.posted"]
        digest = build_digest(events, window_start=start, window_end=now)
        text = render_digest(digest)
        if services.chat is not None and services.settings.slack_team_channel:
            try:
                ref = await services.chat.post(
                    services.settings.slack_team_channel,
                    text,
                    attribution="posted by Relay (team digest)",
                )
                digest.channel, digest.message_ref = ref.channel, ref.ts
            except Exception:
                pass
        ev = Event.new(
            digest,
            actor=DIGEST_ACTOR,
            source="api",
            thread_id=f"digest:{now:%Y-%m-%d}",
            provenance=Provenance(parent_event_ids=digest.cited_event_ids[:200]),
        )
        await store.append(ev)
        await session.commit()
    if services.memory is not None or services.embedder is not None:  # in a worker: index directly
        from relayagents.workers.jobs import _index

        await _index(services, [ev])
    else:
        await index_later(services, [ev])
    return ev
