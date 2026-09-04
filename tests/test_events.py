"""Every event type serializes, round-trips, and rejects drift."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from relayagents.core import events as ev
from relayagents.core.events import EVENT_TYPES, PAYLOAD_TYPES, Actor, Event, Provenance

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

SAMPLES: dict[str, ev.Payload] = {
    "meeting.started": ev.MeetingStarted(
        meeting_id="mtg_1", title="Sync", participants=["ada", "grace"], started_at=NOW
    ),
    "transcript.segment": ev.TranscriptSegment(
        meeting_id="mtg_1",
        segment_id="seg_1",
        speaker="ada",
        start_s=0,
        end_s=3.5,
        text="hello",
        confidence=0.9,
    ),
    "decision.made": ev.DecisionMade(
        decision_id="dec_1",
        statement="Use pgvector",
        topic="storage",
        decided_by=["ada"],
        supersedes="dec_0",
    ),
    "action_item.created": ev.ActionItemCreated(
        item_id="item_1", title="Add cache table", assignee="grace", due=NOW, meeting_id="mtg_1"
    ),
    "action_item.updated": ev.ActionItemUpdated(
        item_id="item_1", status="blocked", note="waiting on migration review"
    ),
    "action_item.closed": ev.ActionItemClosed(
        item_id="item_1", resolution="done", links=["https://github.com/relayagents/relay/pull/1"]
    ),
    "question.opened": ev.QuestionOpened(
        question_id="q_1", text="Budget for annotation?", asked_of="linus"
    ),
    "question.answered": ev.QuestionAnswered(question_id="q_1", answer="Yes, 100 queries."),
    "report.posted": ev.ReportPosted(text="Shipped the migration", item_id="item_1", links=[]),
    "tool.called": ev.ToolCalled(
        call_id="call_1", tool="report", transport="mcp", arguments={"text": "x"}
    ),
    "tool.result": ev.ToolResult(call_id="call_1", tool="report", ok=True, duration_ms=12),
    "agent.message": ev.AgentMessage(
        task_id="task_1",
        from_agent="ada.hermes",
        to_agent="grace.hermes",
        text="Can you take item_1?",
        surfaced_to=["grace"],
    ),
    "approval.requested": ev.ApprovalRequested(
        approval_id="apr_1",
        action="open GitHub issue",
        action_type="github.issue.create",
        requested_of="grace",
    ),
    "approval.resolved": ev.ApprovalResolved(
        approval_id="apr_1", decision="approved", resolved_by="grace"
    ),
    "standup.posted": ev.StandupPosted(
        user_id="grace", mode="draft", done=["migration"], cited_event_ids=["evt_1"]
    ),
    "digest.posted": ev.DigestPosted(
        window_start=NOW, window_end=NOW, shipped=["migration"], quiet=False
    ),
}


def test_every_type_has_a_sample() -> None:
    assert set(SAMPLES) == set(EVENT_TYPES) == set(PAYLOAD_TYPES)
    assert len(EVENT_TYPES) == 16


@pytest.mark.parametrize("etype", EVENT_TYPES)
def test_round_trip(etype: str) -> None:
    payload = SAMPLES[etype]
    e = Event.new(
        payload,
        actor=Actor.human("ada"),
        source="meeting",
        thread_id="thr",
        provenance=Provenance(segment_ids=["seg_1"]),
        ts=NOW,
    )
    assert e.type == etype
    raw = e.to_json()
    back = Event.from_json(raw)
    assert back == e
    assert type(back.payload) is type(payload)
    # dict round trip as the DB does it
    again = Event.model_validate({**e.model_dump(mode="json")})
    assert again == e


def test_type_is_filled_from_payload() -> None:
    e = Event.model_validate(
        {
            "actor": {"kind": "system", "id": "relay"},
            "source": "api",
            "payload": {"type": "report.posted", "text": "hi"},
        }
    )
    assert e.type == "report.posted"
    assert e.id.startswith("evt_")
    assert e.ts.tzinfo is not None


def test_type_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "type": "decision.made",
                "actor": {"kind": "system", "id": "relay"},
                "source": "api",
                "payload": {"type": "report.posted", "text": "hi"},
            }
        )


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "actor": {"kind": "system", "id": "relay"},
                "source": "api",
                "payload": {"type": "meeting.ended", "meeting_id": "x"},
            }
        )


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ev.ReportPosted(text="x", secret="y")  # type: ignore[call-arg]


def test_visibility_has_no_private_scope() -> None:
    with pytest.raises(ValidationError):
        Event.new(
            ev.ReportPosted(text="x"), actor=Actor.human("ada"), source="api", visibility="private"
        )  # type: ignore[arg-type]


def test_actor_user_id() -> None:
    assert Actor.agent("ada.hermes").user_id == "ada"
    assert Actor.human("ada").user_id == "ada"
