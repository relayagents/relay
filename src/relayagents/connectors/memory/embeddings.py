"""Event embeddings for the pgvector leg of `recall`. **Workers only.**

The embedder runs on the team's embedding model through Pydantic AI (any provider it knows,
``provider:model``). relay-api never constructs it: query-time embedding happens in the
``semantic_recall`` job so the team key stays in the workers (ADR-0002).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from relayagents.core.config import Settings
from relayagents.core.db import Database
from relayagents.core.events import Event
from relayagents.core.protocols import Embedder
from relayagents.core.store import EventStore, flatten_text

log = structlog.get_logger()
BATCH = 100
MAX_CHARS = 6000  # conservative for 8k-token models even on CJK/code


class PydanticAIEmbedder:
    """``core.protocols.Embedder`` on ``pydantic_ai.embeddings.Embedder``."""

    def __init__(self, model: object, *, dim: int) -> None:
        from pydantic_ai.embeddings import Embedder, EmbeddingSettings

        self.model_name = (
            model if isinstance(model, str) else getattr(model, "model_name", "custom")
        )
        self.dim = dim  # RELAY_EMBEDDING_DIM: the width events.embedding is sized to
        self._embedder = Embedder(model, settings=EmbeddingSettings(truncate=True))  # type: ignore[arg-type]

    async def embed_query(self, text: str) -> list[float]:
        result = await self._embedder.embed_query(text[:MAX_CHARS])
        return self._check(list(result.embeddings[0]))

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float] | None]:
        """One vector per input, ``None`` for inputs the model rejected. Providers that ignore
        ``truncate`` (OpenAI does) get a character cap; a failing batch is retried one by one so a
        single bad input never costs the other 99."""
        out: list[list[float] | None] = []
        clipped = [t[:MAX_CHARS] for t in texts]
        for i in range(0, len(clipped), BATCH):
            chunk = clipped[i : i + BATCH]
            try:
                result = await self._embedder.embed_documents(chunk)
                out.extend(self._check(list(v)) for v in result.embeddings)
            except ValueError:
                raise  # wrong dimension: configuration error, not a bad input
            except Exception as exc:
                log.warning("embeddings.batch_failed", error=str(exc)[:200], size=len(chunk))
                for text in chunk:
                    try:
                        one = await self._embedder.embed_documents([text])
                        out.append(self._check(list(one.embeddings[0])))
                    except ValueError:
                        raise
                    except Exception as exc_one:
                        log.warning(
                            "embeddings.item_failed", error=str(exc_one)[:200], chars=len(text)
                        )
                        out.append(None)
        return out

    def _check(self, vec: list[float]) -> list[float]:
        if len(vec) != self.dim:
            raise ValueError(
                f"embedding model {self.model_name!r} returns {len(vec)} dimensions but "
                f"RELAY_EMBEDDING_DIM is {self.dim}. Set RELAY_EMBEDDING_DIM={len(vec)} and run "
                "`relay migrate` (relay-api runs it on start), or pick a model of that width."
            )
        return vec


def make_embedder(settings: Settings) -> PydanticAIEmbedder | None:
    """Build the team embedder, or return None (with a log line saying why) so recall degrades."""
    from pydantic_ai.embeddings import infer_embedding_model

    name = settings.embedding_model.strip()
    if name in ("", "none", "off"):
        log.info("embeddings.disabled", reason="RELAY_EMBEDDING_MODEL is empty")
        return None
    try:
        infer_embedding_model(name)  # fails fast on an unknown provider or a missing key
    except Exception as exc:
        # Any failure to build an embedder degrades recall rather than crash-looping the
        # worker: an unknown provider raises pydantic_ai's UserError, but a missing provider
        # credential raises that provider's own error type (e.g. openai.OpenAIError), which is
        # a longer and less stable list to name explicitly.
        reason = next(iter(str(exc).splitlines()), type(exc).__name__)
        log.warning("embeddings.disabled", model=name, reason=reason)
        return None
    log.info("embeddings.enabled", model=name, dim=settings.embedding_dim)
    return PydanticAIEmbedder(name, dim=settings.embedding_dim)


async def disable_on_column_mismatch(services: Any) -> None:
    """Turn the vector leg off when ``events.embedding`` is not RELAY_EMBEDDING_DIM wide.

    Normally impossible (relay-api runs ``relay migrate`` before workers start), but a worker
    started against an unmigrated column would otherwise fail every write and every query with a
    database error. Off means recall degrades to the lexical leg, with one log line saying why.
    """
    from relayagents.core.embedding_column import embedding_dim_mismatch

    if services.embedder is None:
        return
    try:
        reason = await embedding_dim_mismatch(services.db, services.settings.embedding_dim)
    except Exception as exc:  # DB not reachable yet: keep the embedder, writes will tell
        log.warning("embeddings.column_unchecked", error=str(exc)[:200])
        return
    if reason:
        log.error("embeddings.disabled", reason=reason)
        services.embedder = None


async def embed_events(db: Database, embedder: Embedder, events: Sequence[Event]) -> int:
    """Embed the events that carry text and store the vectors. Returns how many were embedded."""
    from relayagents.connectors.memory import INDEXED_TYPES

    todo = [(e.id, flatten_text(e)) for e in events if e.type in INDEXED_TYPES]
    todo = [(i, t) for i, t in todo if t.strip()]
    if not todo:
        return 0
    vectors = await embedder.embed_documents([t for _, t in todo])
    pairs = [(i, v) for (i, _), v in zip(todo, vectors, strict=True) if v is not None]
    async with db.session() as session:
        await EventStore(session).set_embeddings(pairs)
        await session.commit()
    return len(pairs)
