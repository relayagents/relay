"""Event embeddings for the pgvector leg of `recall`.

Uses the team's embedding model (workers' key only). When no key or a non-OpenAI model is
configured, ``make_embedder`` returns ``None`` and recall silently runs without the vector leg.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from sqlalchemy import update

from relayagents.core.config import Settings
from relayagents.core.db import Database
from relayagents.core.events import Event
from relayagents.core.models import EventRow
from relayagents.core.store import flatten_text

EMBED_TYPES = {
    "decision.made",
    "action_item.created",
    "action_item.closed",
    "question.opened",
    "question.answered",
    "report.posted",
    "transcript.segment",
    "standup.posted",
    "digest.posted",
}


class OpenAIEmbedder:
    def __init__(self, model: str, dim: int) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI()
        self.model = model
        self.dim = dim

    async def __call__(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(
            model=self.model, input=[t[:8000] for t in texts], dimensions=self.dim
        )
        return [d.embedding for d in resp.data]


def make_embedder(settings: Settings) -> OpenAIEmbedder | None:
    provider, _, model = settings.embedding_model.partition(":")
    if provider == "openai" and model and os.environ.get("OPENAI_API_KEY"):
        return OpenAIEmbedder(model, settings.embedding_dim)
    return None


async def embed_events(db: Database, embedder: OpenAIEmbedder, events: Sequence[Event]) -> int:
    """Compute and store embeddings for the events that carry text. Returns how many were embedded."""
    todo = [(e, flatten_text(e)) for e in events if e.type in EMBED_TYPES]
    todo = [(e, t) for e, t in todo if t]
    if not todo:
        return 0
    vectors = await embedder.embed_many([t for _, t in todo])
    async with db.session() as session:
        for (e, _), vec in zip(todo, vectors, strict=True):
            await session.execute(update(EventRow).where(EventRow.id == e.id).values(embedding=vec))
        await session.commit()
    return len(todo)
