"""Topic reuse and supersedes detection happen in the extractor, not in a graph (ADR-0005)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relayagents.core import projections
from relayagents.core.events import Actor, DecisionMade, Event
from relayagents.core.models import MeetingRow
from relayagents.core.protocols import ExtractionContext, RecentDecision, Segment, Transcript
from relayagents.core.store import EventStore
from relayagents.workers.extraction import (
    ExtractionResult,
    KeywordExtractor,
    LLMExtractor,
    normalize_topic,
)
from relayagents.workers.jobs import extract_meeting, extraction_context
from tests.conftest import FIXTURES

CTX = ExtractionContext(
    known_topics=["embedding cache", "paper deadline"],
    recent_decisions=[
        RecentDecision(
            decision_id="dec_old",
            topic="paper deadline",
            statement="the paper deadline stays at October 3",
        ),
        RecentDecision(
            decision_id="dec_cache",
            topic="embedding cache",
            statement="we cache embeddings in pgvector",
        ),
    ],
)


def test_normalize_topic_reuses_known_names() -> None:
    known = ["embedding cache", "paper deadline"]
    assert normalize_topic("Embedding Cache", known) == "embedding cache"
    assert normalize_topic("eval cache embeddings", known) == "embedding cache"
    assert normalize_topic("deadline for the paper", known) == "paper deadline"
    assert normalize_topic("gpu budget", known) == "gpu budget"
    assert normalize_topic(None, known) is None


async def test_keyword_extractor_supersedes_on_cue_and_topic() -> None:
    t = Transcript(
        meeting_id="m",
        segments=[
            Segment(
                segment_id="s1",
                speaker="ada",
                start_s=0,
                end_s=5,
                text="Decision: the paper deadline moved to October 10 instead of October 3.",
            ),
            Segment(
                segment_id="s2",
                speaker="ada",
                start_s=5,
                end_s=9,
                text="Decision: we will also cache reranker scores.",
            ),
        ],
    )
    events = [
        e
        async for e in KeywordExtractor().extract(
            t, meeting_id="m", participants=["ada"], context=CTX
        )
    ]
    d1, d2 = (e.payload for e in events)
    assert d1.topic == "paper deadline" and d1.supersedes == "dec_old"  # type: ignore[attr-defined]
    assert d2.supersedes is None  # no cue phrase, no supersede  # type: ignore[attr-defined]


async def test_llm_extractor_gets_context_and_may_supersede() -> None:
    seen: dict[str, str] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["prompt"] = repr(messages)  # pydantic-ai messages are dataclasses
        canned = ExtractionResult.model_validate(
            {
                "decisions": [
                    {
                        "statement": "Deadline is October 10",
                        "topic": "Paper Deadline",
                        "supersedes": "dec_old",
                        "segment_ids": ["seg_0009"],
                    },
                    {
                        "statement": "Hallucinated link",
                        "topic": "x",
                        "supersedes": "dec_nope",
                        "segment_ids": ["seg_0001"],
                    },
                ]
            }
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=canned.model_dump())]
        )

    t = Transcript.model_validate(json.loads((FIXTURES / "transcript_sample.json").read_text()))
    events = [
        e
        async for e in LLMExtractor(FunctionModel(respond)).extract(
            t, meeting_id="m", participants=["ada"], context=CTX
        )
    ]
    assert "dec_old" in seen["prompt"] and "embedding cache" in seen["prompt"]
    assert events[0].payload.topic == "paper deadline" and events[0].payload.supersedes == "dec_old"  # type: ignore[attr-defined]
    assert events[1].payload.supersedes is None  # unknown id dropped  # type: ignore[attr-defined]


async def test_extraction_context_comes_from_projections_and_flows_end_to_end(
    services, team
) -> None:  # type: ignore[no-untyped-def]
    async with services.db.session() as session:
        old = Event.new(
            DecisionMade(
                decision_id="dec_old",
                statement="the paper deadline stays at October 3",
                topic="paper deadline",
            ),
            actor=Actor.human("ada"),
            source="meeting",
        )
        await EventStore(session).append(old)
        await projections.apply(session, old)
        ctx = await extraction_context(session)
        assert (
            ctx.known_topics == ["paper deadline"]
            and ctx.recent_decisions[-1].decision_id == "dec_old"
        )
        path = services.settings.data_dir / "m2" / "transcript.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            Transcript(
                meeting_id="x",
                segments=[
                    Segment(
                        segment_id="s1",
                        speaker="ada",
                        start_s=0,
                        end_s=5,
                        text="Decision: the paper deadline moved to October 10 instead.",
                    )
                ],
            ).model_dump_json()
        )
        session.add(
            MeetingRow(
                id="mtg_2",
                title="Follow-up",
                status="queued",
                transcript_path=str(path),
                participants=["ada"],
                created_by="ada",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    await extract_meeting({"services": services}, "mtg_2")
    async with services.db.session() as session:
        ds = {d.id: d for d in await projections.list_decisions(session)}
        assert ds["dec_old"].superseded_by is not None
        new = ds[ds["dec_old"].superseded_by]
        assert new.supersedes == "dec_old" and new.topic == "paper deadline"
        ctx = await extraction_context(session)
        assert [d.decision_id for d in ctx.recent_decisions] == [new.id]  # superseded ones drop out
