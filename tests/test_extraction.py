from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relayagents.api.a2a_broker import broker
from relayagents.core.models import MeetingRow
from relayagents.core.protocols import Transcript
from relayagents.core.store import EventStore
from relayagents.tools.context import Services
from relayagents.workers.extraction import ExtractionResult, KeywordExtractor, LLMExtractor
from relayagents.workers.jobs import extract_meeting
from tests.conftest import FIXTURES

PARTICIPANTS = ["ada", "grace", "linus"]


def load_fixture() -> Transcript:
    return Transcript.model_validate(json.loads((FIXTURES / "transcript_sample.json").read_text()))


def test_keyword_extractor_on_fixture() -> None:
    result = KeywordExtractor(PARTICIPANTS).extract_sync(load_fixture())
    assert [d.statement for d in result.decisions] == [
        "we cache embeddings in pgvector, keyed by model name and content hash",
        "the paper deadline stays at October 3, no extension request",
    ]
    assert result.decisions[0].segment_ids == ["seg_0004"]
    assert [(a.assignee, a.title) for a in result.action_items] == [
        ("grace", "add the embedding cache table and the migration by Friday"),
        ("linus", "wire the nightly eval to read from the cache once the table lands"),
    ]
    assert result.questions[0].asked_of == "linus" and result.questions[0].segment_ids == [
        "seg_0007"
    ]


async def test_keyword_extractor_emits_events_with_provenance() -> None:
    events = [
        e
        async for e in KeywordExtractor().extract(
            load_fixture(), meeting_id="mtg_x", participants=PARTICIPANTS
        )
    ]
    assert [e.type for e in events] == [
        "decision.made",
        "decision.made",
        "action_item.created",
        "action_item.created",
        "question.opened",
    ]
    for e in events:
        assert e.provenance.segment_ids, e
        assert e.thread_id == "mtg_x"


async def test_llm_extractor_drops_items_without_valid_provenance() -> None:
    canned = ExtractionResult.model_validate(
        {
            "decisions": [
                {
                    "statement": "Cache embeddings in pgvector",
                    "topic": "eval cache",
                    "segment_ids": ["seg_0004"],
                },
                {"statement": "Hallucinated", "segment_ids": ["seg_9999"]},
            ],
            "action_items": [
                {
                    "title": "Add cache table",
                    "assignee": "grace",
                    "due": "2026-09-05",
                    "segment_ids": ["seg_0005"],
                }
            ],
            "questions": [
                {
                    "text": "Budget for 100 annotated queries?",
                    "asked_of": "linus",
                    "segment_ids": ["seg_0007"],
                }
            ],
        }
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.output_tools, "structured output should be requested via a tool"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=canned.model_dump())]
        )

    extractor = LLMExtractor(FunctionModel(respond))
    events = [
        e
        async for e in extractor.extract(
            load_fixture(), meeting_id="mtg_y", participants=PARTICIPANTS
        )
    ]
    assert [e.type for e in events] == ["decision.made", "action_item.created", "question.opened"]
    assert events[1].payload.due == datetime(2026, 9, 5, tzinfo=UTC)  # type: ignore[attr-defined]


async def test_extract_meeting_job_end_to_end(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    """Transcript file → segment events → extraction → projections → PM summary + A2A tasks."""
    path = services.settings.data_dir / "m" / "transcript.json"
    path.parent.mkdir(parents=True)
    path.write_text((FIXTURES / "transcript_sample.json").read_text())
    async with services.db.session() as session:
        session.add(
            MeetingRow(
                id="mtg_e2e",
                title="Retrieval sync",
                status="queued",
                transcript_path=str(path),
                participants=PARTICIPANTS,
                created_by="ada",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

    result = await extract_meeting({"services": services}, "mtg_e2e")
    assert len(result["events"]) == 5
    assert result["dispatch"]["summary_posted"] is True
    assert sorted(t["to"] for t in result["dispatch"]["tasks"]) == ["grace.hermes", "linus.hermes"]

    async with services.db.session() as session:
        store = EventStore(session)
        segs = await store.query(types=["transcript.segment"], thread_id="mtg_e2e", limit=100)
        assert len(segs) == 10
        meeting = await session.get(MeetingRow, "mtg_e2e")
        assert meeting is not None and meeting.status == "done"
        inbox = await broker.inbox(session, "grace.hermes")
        assert len(inbox) == 1 and "add the embedding cache table" in inbox[0].history[0].text
    chat = services.chat
    assert any("Retrieval sync" in p["text"] for p in chat.posts)  # type: ignore[union-attr]
    assert {d["user_id"] for d in chat.dms} == {"grace", "linus"}  # type: ignore[union-attr]
