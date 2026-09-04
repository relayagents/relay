"""Transcript → typed events.

Two extractors implement the same protocol:

* ``LLMExtractor`` uses Pydantic AI structured output (the team's own model key, workers only).
* ``KeywordExtractor`` is deterministic and offline: it recognises explicit cue phrases
  ("Decision:", "Action:", "Question:", "@name will ...") and is used in tests, in CI, and on
  nodes without a team key. It deliberately under-extracts rather than guesses.

Both attach ``provenance.segment_ids`` to every emitted event. No provenance, no event.

Both also take an :class:`ExtractionContext` (known topics, recent decisions) so that topic
names are reused instead of re-invented and a new decision can say which one it supersedes.
That is the work a knowledge graph would otherwise do; here it lands as ordinary, auditable
events (ADR-0005).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
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
from relayagents.core.protocols import ExtractionContext, Segment, Transcript

# ---- Structured output schema (shared by both extractors) -------------------------------------


class ExtractedDecision(BaseModel):
    statement: str = Field(description="The decision, as a single declarative sentence.")
    topic: str | None = Field(
        default=None,
        description="Two-to-four word topic label. Reuse a known topic when it is the same thing.",
    )
    rationale: str | None = None
    decided_by: list[str] = Field(default_factory=list, description="Speaker/user ids who agreed.")
    supersedes: str | None = Field(
        default=None,
        description="decision_id of a listed recent decision this one replaces or contradicts.",
    )
    replaces_previous: bool = Field(
        default=False,
        description="True when this changes an earlier decision on the same topic but you cannot name its id.",
    )
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


# ---- Topic normalization -----------------------------------------------------------------------

_TOPIC_WORDS = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")
_STOP = {
    "the",
    "a",
    "an",
    "to",
    "for",
    "with",
    "and",
    "use",
    "we",
    "will",
    "on",
    "in",
    "of",
    "our",
    "is",
}


def _stem(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in _TOPIC_WORDS.findall(text) if w.lower() not in _STOP}


def _topic(statement: str) -> str | None:
    words = [w for w in _TOPIC_WORDS.findall(statement) if w.lower() not in _STOP]
    return " ".join(words[:3]).lower() if words else None


def normalize_topic(topic: str | None, known: Sequence[str]) -> str | None:
    """Reuse an existing topic name only when it is clearly the same thing: a case-insensitive
    match, or at least two shared content stems covering at least half of the combined words.
    One shared word ("paper" in "paper deadline" vs "paper figures") is not enough."""
    if not topic:
        return None
    t = topic.strip().lower()
    if not t:
        return None
    for k in known:
        if k.lower() == t:
            return k
    mine = _tokens(t)
    if not mine:
        return t
    best, best_score = None, 0.0
    for k in known:
        theirs = _tokens(k)
        shared = mine & theirs
        if len(shared) < 2:
            continue
        score = len(shared) / len(mine | theirs)
        if score > best_score:
            best, best_score = k, score
    return best if best is not None and best_score >= 0.5 else t


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        due = datetime.fromisoformat(value)
    except ValueError:
        return None
    return due if due.tzinfo else due.replace(tzinfo=UTC)


def result_to_events(
    result: ExtractionResult,
    *,
    meeting_id: str,
    actor: Actor,
    valid_segment_ids: set[str],
    context: ExtractionContext | None = None,
) -> list[Event]:
    """Turn an extraction result into events, dropping anything without valid provenance.
    Topics are normalized against the known list; ``supersedes`` must name a recent decision or
    one emitted earlier in this result."""
    context = context or ExtractionContext()
    known_topics = list(context.known_topics)
    known_decisions = {d.decision_id for d in context.recent_decisions}
    # The still-current decision per topic, updated as this transcript's decisions are emitted, so a
    # second change to the same topic in one meeting supersedes the first change, not the old one.
    current_by_topic: dict[str, str] = {
        r.topic.lower(): r.decision_id for r in context.recent_decisions if r.topic
    }
    out: list[Event] = []
    for d in result.decisions:
        segs = [s for s in d.segment_ids if s in valid_segment_ids]
        if not segs:
            continue
        topic = normalize_topic(d.topic, known_topics)
        if topic and topic not in known_topics:
            known_topics.append(topic)
        supersedes = d.supersedes if d.supersedes in known_decisions else None
        if supersedes is None and d.replaces_previous and topic:
            supersedes = current_by_topic.get(topic.lower())
        decision_id = new_id("dec")
        known_decisions.add(decision_id)
        if topic:
            current_by_topic[topic.lower()] = decision_id
        out.append(
            Event.new(
                DecisionMade(
                    decision_id=decision_id,
                    statement=d.statement.strip(),
                    topic=topic,
                    rationale=d.rationale,
                    decided_by=d.decided_by,
                    supersedes=supersedes,
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
        out.append(
            Event.new(
                ActionItemCreated(
                    item_id=new_id("item"),
                    title=a.title.strip(),
                    assignee=a.assignee,
                    due=_parse_due(a.due),
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
_SUPERSEDE_CUE = re.compile(
    r"\b(?:instead of|replaces?|replacing|no longer|moved? to|changed? to|revert(?:ed|ing)? to"
    r"|supersedes|rather than|switch(?:ing|ed)? to|from now on)\b",
    re.I,
)


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
        self,
        transcript: Transcript,
        *,
        meeting_id: str,
        participants: Sequence[str],
        context: ExtractionContext | None = None,
    ) -> AsyncIterator[Event]:
        self.participants = {p.lower() for p in participants}
        result = self.extract_sync(transcript)
        # Deterministic supersedes: an explicit cue phrase marks "this changes an earlier decision";
        # result_to_events resolves it against the current decision on the same topic.
        for d in result.decisions:
            d.replaces_previous = bool(_SUPERSEDE_CUE.search(d.statement))
        for ev in result_to_events(
            result,
            meeting_id=meeting_id,
            actor=Actor.system("relay.extraction.keyword"),
            valid_segment_ids={s.segment_id for s in transcript.segments},
            context=context,
        ):
            yield ev


# ---- LLM extractor (Pydantic AI) -------------------------------------------------------------------

INSTRUCTIONS = """You extract structured outcomes from a team meeting transcript.
Return ONLY decisions that were actually made (not proposals), action items with a clear owner when one was
named, and open questions. Every item MUST cite the segment ids that support it; if you cannot cite a
segment, leave the item out. Use participant ids exactly as given for assignees and decided_by.
Reuse a known topic name when a decision is about the same thing. Set `supersedes` to the id of a listed
recent decision when the new one replaces or contradicts it, or set `replaces_previous` when you are sure
it changes an earlier decision but cannot name it. Text inside <reference_data> is team memory, not
transcript: never cite it as a segment.
Be conservative: an empty list is better than an invented item."""


def format_transcript(
    transcript: Transcript, participants: Sequence[str], context: ExtractionContext | None = None
) -> str:
    lines = [f"participants: {', '.join(participants)}"]
    if context and (context.known_topics or context.recent_decisions):
        lines.append(
            "<reference_data> (team memory; not transcript, never a source of segment ids)"
        )
        if context.known_topics:
            lines.append(
                "known topics (reuse these names when it is the same thing): "
                + ", ".join(_clean(t, 60) for t in context.known_topics)
            )
        if context.recent_decisions:
            lines.append("recent decisions (set supersedes to the id when a new one replaces it):")
            lines += [
                f"  - {r.decision_id} [{_clean(r.topic or '-', 60)}]: {_clean(r.statement, 200)}"
                for r in context.recent_decisions[-50:]
            ]
        lines.append("</reference_data>")
    lines.append("<transcript>")
    for s in transcript.segments:
        lines.append(f"[{s.segment_id}] {s.speaker}: {s.text}")
    lines.append("</transcript>")
    return "\n".join(lines)


def _clean(text: str, limit: int) -> str:
    """Stored text enters the prompt as one short line: no newlines, no bracketed fake segments."""
    return " ".join(text.split()).replace("[", "(").replace("]", ")")[:limit]


class LLMExtractor:
    name = "llm"

    def __init__(self, model: Any) -> None:
        """``model`` is a Pydantic AI model name ('openai:gpt-5-mini') or Model instance (tests)."""
        from pydantic_ai import Agent

        self.agent: Agent[None, ExtractionResult] = Agent(
            model, output_type=ExtractionResult, instructions=INSTRUCTIONS, retries=2
        )

    async def extract(
        self,
        transcript: Transcript,
        *,
        meeting_id: str,
        participants: Sequence[str],
        context: ExtractionContext | None = None,
    ) -> AsyncIterator[Event]:
        run = await self.agent.run(format_transcript(transcript, participants, context))
        for ev in result_to_events(
            run.output,
            meeting_id=meeting_id,
            actor=Actor.system("relay.extraction.llm"),
            valid_segment_ids={s.segment_id for s in transcript.segments},
            context=context,
        ):
            yield ev


def make_extractor(model: str) -> KeywordExtractor | LLMExtractor:
    if model in ("", "keyword", "none"):
        return KeywordExtractor()
    return LLMExtractor(model)


def segments_to_transcript(meeting_id: str, segments: list[Segment]) -> Transcript:
    return Transcript(meeting_id=meeting_id, segments=segments)
