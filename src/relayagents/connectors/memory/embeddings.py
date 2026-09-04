"""Event embeddings for the pgvector leg of `recall`. **Workers only.**

The embedder runs on the team's embedding model through Pydantic AI (any provider it knows,
``provider:model``). relay-api never constructs it: query-time embedding happens in the
``semantic_recall`` job so the team key stays in the workers (ADR-0002).
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from relayagents.core.config import Settings
from relayagents.core.db import Database
from relayagents.core.events import Event
from relayagents.core.models import EMBEDDING_DIM
from relayagents.core.store import EventStore, flatten_text

log = structlog.get_logger()
BATCH = 100


class PydanticAIEmbedder:
    """``core.protocols.Embedder`` on ``pydantic_ai.embeddings.Embedder``."""

    def __init__(self, model: object) -> None:
        from pydantic_ai.embeddings import Embedder, EmbeddingSettings

        self.model_name = (
            model if isinstance(model, str) else getattr(model, "model_name", "custom")
        )
        self._embedder = Embedder(model, settings=EmbeddingSettings(truncate=True))  # type: ignore[arg-type]

    async def embed_query(self, text: str) -> list[float]:
        result = await self._embedder.embed_query(text)
        return _check(list(result.embeddings[0]), self.model_name)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            result = await self._embedder.embed_documents(list(texts[i : i + BATCH]))
            out.extend(_check(list(v), self.model_name) for v in result.embeddings)
        return out


def _check(vec: list[float], model: str) -> list[float]:
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding model {model!r} returns {len(vec)} dimensions; the events.embedding column is {EMBEDDING_DIM}. Pick a {EMBEDDING_DIM}-d model or add a migration."
        )
    return vec


def make_embedder(settings: Settings) -> PydanticAIEmbedder | None:
    """Build the team embedder, or return None (with a log line saying why) so recall degrades."""
    from pydantic_ai.embeddings import UserError, infer_embedding_model

    name = settings.embedding_model.strip()
    if name in ("", "none", "off"):
        log.info("embeddings.disabled", reason="RELAY_EMBEDDING_MODEL is empty")
        return None
    try:
        infer_embedding_model(name)  # fails fast on an unknown provider or a missing key
    except (UserError, ValueError) as exc:
        log.warning("embeddings.disabled", model=name, reason=str(exc).splitlines()[0])
        return None
    log.info("embeddings.enabled", model=name)
    return PydanticAIEmbedder(name)


async def embed_events(db: Database, embedder: object, events: Sequence[Event]) -> int:
    """Embed the events that carry text and store the vectors. Returns how many were embedded."""
    from relayagents.connectors.memory import INDEXED_TYPES

    todo = [(e.id, flatten_text(e)) for e in events if e.type in INDEXED_TYPES]
    todo = [(i, t) for i, t in todo if t.strip()]
    if not todo:
        return 0
    vectors = await embedder.embed_documents([t for _, t in todo])  # type: ignore[attr-defined]
    async with db.session() as session:
        await EventStore(session).set_embeddings(
            list(zip([i for i, _ in todo], vectors, strict=True))
        )
        await session.commit()
    return len(todo)
