# ADR-0005: Team memory is the event log, projections, and pgvector. No graph by default.

**Status:** accepted · 2026-09-04 (supersedes the 2026-09-03 version, "Graphiti on embedded Kuzu")

## Context

`recall` must answer questions like "what did we decide about the eval cache, and is that still
current?" with provenance. The first version of this ADR chose a temporal knowledge graph
(Graphiti on embedded Kuzu) as a third derived store next to projections and pgvector. Reviewing
that choice against what Relay actually holds changed the answer.

**What is strongly linked, and by what.** Nearly every link that matters is already an explicit id
in the event log, written by the code that knows the fact, not inferred afterwards:

| Link | Where it lives |
|---|---|
| meeting → segments → decisions, items, questions | `thread_id`, `provenance.segment_ids` |
| item → assignee → reports → closure → PR | `item_id` on reports, `links` on closure |
| question → who was asked → answer → agent thread | `question_id`, `asked_of`, A2A `context_id` |
| decision → the decision it replaces | `supersedes` |

Only two links are not structural: **topics as entities** (three meetings calling one thing "the
cache", "embedding cache", and "eval cache") and **contradiction between decisions** when nobody
said "this replaces that". Those are the graph's real jobs; everything else it would extract is a
fuzzier copy of what events already state.

**Eager versus lazy synthesis.** A knowledge graph is LLM synthesis done eagerly at index time.
Handing the retrieved events to the asking agent is the same synthesis done lazily at query time.
Eager buys one shared reading, cheap queries, and a standing answer to "which facts are stale".
Lazy buys simplicity and flexibility and costs tokens per question. At a lab's scale the log is
small: five people produce a few hundred events a day, dominated by transcript segments, a few
million tokens a year. Keyword plus vector retrieval with the structural links attached gets an
agent to the right dozen events for almost any question. Eager wins only when questions routinely
span many meetings and the team is large enough that re-synthesis is expensive or inconsistent.

## Decision

1. **No graph by default.** `RELAY_MEMORY_BACKEND=none`. The `MemoryStore` protocol and the
   Graphiti connector stay as an opt-in (`--extra graph`, `graphiti-kuzu`) for a team that outgrows
   this; they are not installed in the default image.
2. **The graph's two jobs move into the extractor**, where they land as ordinary, auditable events.
   Before reading a transcript the extractor is given an `ExtractionContext`: the known topic names
   and the recent decisions that are still current. It reuses a known topic when the new one is the
   same thing, and sets `supersedes` when a new decision replaces or contradicts a listed one. The
   deterministic extractor does this with an explicit cue phrase ("instead of", "moved to", "no
   longer", ...) plus a matching topic; the LLM extractor is asked to.
3. **`recall` returns links, not just text.** Every hit carries the event type, actor, thread, and
   the ids it points at (item, decision, supersedes, source segments, parent events), so the asking
   agent can walk the structure without a graph query language.
4. **Stores, restated.** Postgres holds the truth (`events`) and the projections
   (`action_items`, `decisions` with `supersedes`/`superseded_by`). pgvector holds one embedding per
   event with text, written by the workers with the team key. That is the whole memory system.

## Alternatives considered

- **Graphiti on Kuzu** (the previous decision). Kuzu's upstream stopped in October 2025; keeping it
  as the default meant shipping an unmaintained dependency for a benefit we could not point at.
- **Graphiti on FalkorDB Lite or a FalkorDB container.** Maintained, and the right choice if a graph
  is ever wanted; it is what the opt-in connector should move to when that day comes.
- **Neo4j.** Heaviest option; wrong for a €10 VPS.
- **Apache AGE inside Postgres.** Keeps one database, but no extraction library targets it, so
  Relay would own entity extraction. Not worth it for two link types.
- **Dropping pgvector too, keyword only.** Tempting for a tiny team, but embeddings are cheap, live
  in the same Postgres, and catch paraphrase that keyword search misses. Kept.
- **SQLite instead of Postgres for single-node installs.** Possible later (sqlite-vec exists), but
  Redis is required for the queue anyway, so Postgres in the same compose file costs little.

## Revisit if

Someone asks a cross-meeting question that `recall` plus their agent answers wrongly because the
relevant facts were never linked; topic or supersedes resolution in the extractor proves too weak
in practice; or the team grows past the point where every agent re-synthesizing the same history
is noticeably slow or inconsistent. Any of these makes the opt-in graph (on FalkorDB Lite, not
Kuzu) worth turning on.
