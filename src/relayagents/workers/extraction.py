"""Transcript → typed events.

Two extractors implement the same protocol:

* ``LLMExtractor`` uses Pydantic AI structured output (the team's own model key, workers only).
* ``KeywordExtractor`` is deterministic and offline: it recognises explicit cue phrases
  ("Decision:", "Action:", "Question:", "@name will ...") and is used in tests, in CI, and on
  nodes without a team key. It deliberately under-extracts rather than guesses.

Both attach ``provenance.segment_ids`` to every emitted event. No provenance, no event.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic import BaseModel, Field

from relayagents.core.events import (
    ActionItemCreated,
    Actor,
    DecisionMade,
    Event,
    Provenance,
    QuestionOpened,
)
from relayagents.core.ids import new_id
from relayagents.core.protocols import Segment, Transcript

# ---- Structured output schema (shared by both extractors) -------------------------------------


class ExtractedDecision(BaseModel):
    statement: str = Field(description="The decision, as a single declarative sentence.")
    topic: str | None = Field(default=None, description="Two-to-four word topic label.")
    rationale: str | None = None
    decided_by: list[str] = Field(default_factory=list, description="Speaker/user ids who agreed.")
    segment_ids: list[str] = Field(description="Transcript segments that support this. Required.")


class ExtractedActionItem(BaseModel):
    title: str
    assignee: str | None = Field(
        default=None, description="User id if clearly assigned, else null."
    )
    due: str | None = Field(default=None, description="ISO date if stated, else null.")
    details: str | None = None
    segment_ids: list[str]


class ExtractedQuestion(BaseModel):
    text: str
    asked_of: str | None = None
    segment_ids: list[str]


class ExtractionResult(BaseModel):
    decisions: list[ExtractedDecision] = Field(default_factory=list)
    action_items: list[ExtractedActionItem] = Field(default_factory=list)
    questions: list[ExtractedQuestion] = Field(default_factory=list)


def result_to_events(
    result: ExtractionResult, *, meeting_id: str, actor: Actor, valid_segment_ids: set[str]
) -> list[Event]:
    """Turn an extraction result into events, dropping anything without valid provenance."""
    out: list[Event] = []
    for d in result.decisions:
        segs = [s for s in d.segment_ids if s in valid_segment_ids]
        if not segs:
            continue
        out.append(
            Event.new(
                DecisionMade(
                    decision_id=new_id("dec"),
                    statement=d.statement.strip(),
                    topic=d.topic,
                    rationale=d.rationale,
                    decided_by=d.decided_by,
                ),
                actor=actor,
                source="meeting",
                thread_id=meeting_id,
                provenance=Provenance(segment_ids=segs),
            )
        )
    for a in result.action_items:
        segs = [s for s in a.segment_ids if s in valid_segment_ids]
        if not segs:
            continue
        due = None
        if a.due:
            from datetime import UTC, datetime

            try:
                due = datetime.fromisoformat(a.due)
                due = due if due.tzinfo else due.replace(tzinfo=UTC)
            except ValueError:
                due = None
        out.append(
            Event.new(
                ActionItemCreated(
                    item_id=new_id("item"),
                    title=a.title.strip(),
                    assignee=a.assignee,
                    due=due,
                    details=a.details,
                    meeting_id=meeting_id,
                ),
                actor=actor,
                source="meeting",
                thread_id=meeting_id,
                provenance=Provenance(segment_ids=segs),
            )
        )
    for q in result.questions:
        segs = [s for s in q.segment_ids if s in valid_segment_ids]
        if not segs:
            continue
        out.append(
            Event.new(
                QuestionOpened(question_id=new_id("q"), text=q.text.strip(), asked_of=q.asked_of),
                actor=actor,
                source="meeting",
                thread_id=meeting_id,
                provenance=Provenance(segment_ids=segs),
            )
        )
    return out


# ---- Keyword extractor (deterministic) -----------------------------------------------------------

_FILLER = r"^\s*(?:(?:ok|okay|also|so|and|fine|right|then|alright)[,\s]+)*"
_DECISION = re.compile(
    _FILLER + r"(?:decision|decided|we decided|let'?s go with)\s*[:\-]\s*(?P<body>.+)$", re.I
)
_ACTION = re.compile(
    _FILLER
    + r"(?:action(?: item)?|todo|ai)\s*[:\-]\s*(?:@?(?P<who>[a-z][a-z0-9_-]*)\s*(?:will|to|:)\s+)?(?P<body>.+)$",
    re.I,
)
_WILL = re.compile(r"^\s*@?(?P<who>[a-z][a-z0-9_-]*)\s+will\s+(?P<body>.+)$", re.I)
_QUESTION = re.compile(
    _FILLER
    + r"(?:open )?question(?:\s+for\s+@?(?P<who>[a-z][a-z0-9_-]*))?\s*[:\-]\s*(?P<body>.+)$",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_TOPIC_WORDS = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")


def _topic(statement: str) -> str | None:
    words = [
        w
        for w in _TOPIC_WORDS.findall(statement)
        if w.lower()
        not in {"the", "a", "an", "to", "for", "with", "and", "use", "we", "will", "on", "in", "of"}
    ]
    return " ".join(words[:3]).lower() if words else None


class KeywordExtractor:
    name = "keyword"

    def __init__(self, participants: Sequence[str] = ()) -> None:
        self.participants = {p.lower() for p in participants}

    def extract_sync(self, transcript: Transcript) -> ExtractionResult:
        result = ExtractionResult()
        for seg in transcript.segments:
            for line in _SENTENCE.split(seg.text.replace("\n", " ")):
                if m := _DECISION.match(line):
                    result.decisions.append(
                        ExtractedDecision(
                            statement=m["body"].rstrip(".").strip(),
                            topic=_topic(m["body"]),
                            decided_by=[seg.speaker],
                            segment_ids=[seg.segment_id],
                        )
                    )
                elif m := _QUESTION.match(line):
                    result.questions.append(
                        ExtractedQuestion(
                            text=m["body"].strip(),
                            asked_of=self._who(m.group("who")),
                            segment_ids=[seg.segment_id],
                        )
                    )
                elif m := _ACTION.match(line):
                    result.action_items.append(
                        ExtractedActionItem(
                            title=m["body"].rstrip(".").strip(),
                            assignee=self._who(m.group("who")),
                            segment_ids=[seg.segment_id],
                        )
                    )
                elif (m := _WILL.match(line)) and self.participants:
                    who = self._who(m.group("who"))
                    if who:
                        result.action_items.append(
                            ExtractedActionItem(
                                title=m["body"].rstrip(".").strip(),
                                assignee=who,
                                segment_ids=[seg.segment_id],
                            )
                        )
        return result

    def _who(self, who: str | None) -> str | None:
        if not who:
            return None
        w = who.lower()
        return w if not self.participants or w in self.participants else None

    async def extract(
        self, transcript: Transcript, *, meeting_id: str, participants: Sequence[str]
    ) -> AsyncIterator[Event]:
        self.participants = {p.lower() for p in participants}
        result = self.extract_sync(transcript)
        for ev in result_to_events(
            result,
            meeting_id=meeting_id,
            actor=Actor.system("relay.extraction.keyword"),
            valid_segment_ids={s.segment_id for s in transcript.segments},
        ):
            yield ev


# ---- LLM extractor (Pydantic AI) -------------------------------------------------------------------

INSTRUCTIONS = """You extract structured outcomes from a team meeting transcript.
Return ONLY decisions that were actually made (not proposals), action items with a clear owner when one was
named, and open questions. Every item MUST cite the segment ids that support it; if you cannot cite a
segment, leave the item out. Use participant ids exactly as given for assignees and decided_by.
Be conservative: an empty list is better than an invented item."""


def format_transcript(transcript: Transcript, participants: Sequence[str]) -> str:
    lines = [f"participants: {', '.join(participants)}", ""]
    for s in transcript.segments:
        lines.append(f"[{s.segment_id}] {s.speaker}: {s.text}")
    return "\n".join(lines)


class LLMExtractor:
    name = "llm"

    def __init__(self, model: Any) -> None:
        """``model`` is a Pydantic AI model name ('openai:gpt-5-mini') or Model instance (tests)."""
        from pydantic_ai import Agent

        self.agent: Agent[None, ExtractionResult] = Agent(
            model, output_type=ExtractionResult, instructions=INSTRUCTIONS, retries=2
        )

    async def extract(
        self, transcript: Transcript, *, meeting_id: str, participants: Sequence[str]
    ) -> AsyncIterator[Event]:
        run = await self.agent.run(format_transcript(transcript, participants))
        for ev in result_to_events(
            run.output,
            meeting_id=meeting_id,
            actor=Actor.system("relay.extraction.llm"),
            valid_segment_ids={s.segment_id for s in transcript.segments},
        ):
            yield ev


def make_extractor(model: str) -> KeywordExtractor | LLMExtractor:
    if model in ("", "keyword", "none"):
        return KeywordExtractor()
    return LLMExtractor(model)


def segments_to_transcript(meeting_id: str, segments: list[Segment]) -> Transcript:
    return Transcript(meeting_id=meeting_id, segments=segments)
