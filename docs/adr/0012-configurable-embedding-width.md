# ADR-0012: The embedding width is configuration, applied by `relay migrate`

**Status:** accepted · 2026-11-03

## Context

`events.embedding` was created as `vector(1536)` by migration 0001, and `EMBEDDING_DIM = 1536` in
`core/models.py` was checked against every vector the embedder returned. That fits OpenAI's
`text-embedding-3-small` and nothing else. Local embedding models served through Ollama or vLLM
(the cheaper route in roadmap item 2, issue #25) usually return 768 or 1024 dimensions, so a team
pointing `OPENAI_BASE_URL` at one got an error on every embed and lost the vector leg of `recall`.

Three facts shape the fix:

- The width depends on which model the team runs, so it is configuration, not schema history. One
  Alembic revision runs once per database, but the team can change models at any time.
- Vectors are derived data (ADR-0001). Vectors from one model cannot be converted to another
  model's width, but they can always be rebuilt from the log.
- relay-api never holds a model key (ADR-0002), and relay-api's container is what runs
  `relay migrate` on start. So the width cannot be found by asking the model during a migration.

## Decision

1. **`RELAY_EMBEDDING_DIM`** (default 1536, allowed range 1 to 2000) states the width. It goes
   to every service, because relay-api applies it and the workers check against it. The ORM
   declares the column as an unsized `vector`. The width lives only on the database column.
2. **`relay migrate` sets the column to that width** after the Alembic upgrade
   (`core/embedding_column.py`). If the width already matches, it does nothing. If not, it does
   all of the following in one transaction: drops the HNSW index, changes the column to the new
   `vector(N)` (`USING NULL`, so stored vectors are dropped), and recreates the index. The column
   is empty by then, so the index build takes no time. relay-api runs `relay migrate` on every
   start, so changing a team's model is: set both variables, restart, and let the workers
   re-embed. The `embed_backlog` sweep does that gradually; `relay replay --rebuild-graph` does it
   all at once.
3. **Mismatches fail loudly, in one place each.** The embedder rejects a vector of the wrong
   width, and the error names the variable to set. A worker that starts against a column of
   another width (for example, a database migrated only with `alembic upgrade head`) turns off
   the vector leg and logs why. Recall then falls back to the keyword leg, instead of a database
   error on every write.
4. Upgrading needs nothing. Existing installs already have `vector(1536)`, which is the default,
   so `relay migrate` leaves them as they are.

## Alternatives

- **A new Alembic revision that reads the setting.** Covers only the first change. A second model
  switch would need another revision, or a hand-written `ALTER`.
- **An unsized column, with a per-width expression index**
  (`USING hnsw ((embedding::vector(N)) ...)`). The column would never change, but every query would
  need the matching cast, and the index would still depend on N. That is more moving parts for
  the same outcome.
- **Detect the width by embedding a probe string.** No second setting would be needed. But only
  the workers can call the model, while relay-api is what migrates, and a probe at startup adds a
  paid call and a startup dependency on the provider.
- **Request the configured width from the provider** (`EmbeddingSettings.dimensions`). This works
  for OpenAI's `text-embedding-3-*`, but `ada-002` and many local servers reject the parameter.
  Teams that want a shortened OpenAI embedding can still pick a model whose native width matches.
- **Re-size automatically from the workers.** That would put schema changes in two services that
  start in either order. Keeping it in `relay migrate`, which relay-api runs before the workers
  start (compose `depends_on`), gives one writer.

## Revisit if

- A team needs more than 2000 dimensions (for example, `text-embedding-3-large` at its full 3072).
  Then store `halfvec`, which pgvector indexes up to 4000.
- Teams switch between models of the same width. `relay migrate` cannot tell those vectors apart.
  The documented step is `relay replay --rebuild-graph`. If that gets missed in practice, record
  the model name next to the width and re-embed when either one changes.
- Re-embedding a large log through `embed_backlog` (200 events every 15 minutes) is too slow
  after a switch. Then the sweep should loop until the backlog is empty.
