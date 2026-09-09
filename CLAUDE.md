# Relay: working notes for coding agents and humans

Relay connects each teammate's coding agent to the team's meetings and chat: meetings become
decisions and action items with provenance, items reach the assignee's own agent, and what the
agent does flows back to the team as reports, standups, and approvals. Relay is the shared memory
and switchboard underneath the agents people already use; it is **not** an agent framework and it
runs no model calls on a user's behalf.

This repo is **public**. Assume every commit is read by strangers.

## Non-negotiables (read before changing anything)

1. **The event log is the truth.** Anything that changes state is an appended `Event`
   (`src/relayagents/core/events.py`). Projections, embeddings, digests are derived and must be
   rebuildable by `relay replay`. Never mutate history; supersede it with a new event.
2. **Per-user permission.** Agents act under their human's own token. Only human tokens mint
   tokens, change identity bindings, or resolve approvals. External writes are approval-gated
   (`core/permissions.py`, mirrored in `docs/permissions.md`; a test keeps them in sync).
3. **relay-api never holds a model key.** Model calls happen in `relay-workers` only. Anything
   that needs a model from the API side goes through a job (see `semantic_recall`).
4. **One tool surface.** Add or change a tool in `src/relayagents/tools/registry.py` only; MCP,
   CLI, and REST are generated from it and `tests/test_tool_surface.py` checks they agree.
5. **Protocol before implementation.** A new connector starts in `core/protocols.py` and
   `docs/protocols.md`, then gets one reference implementation under `connectors/`.
6. **No graph database by default.** Read `docs/adr/0005-team-memory-without-a-graph.md` before
   proposing one; topics and supersedes are the extractor's job.
7. **Never commit secrets, real recordings, or real transcripts.** `.env.example` only; fixtures are
   synthetic. Push protection is on, but it is a backstop, not a plan.
8. **Decisions get an ADR** in `docs/adr/` with alternatives and a "revisit if" section.

## Dev loop

```bash
uv sync --extra graph                      # Python 3.12 venv with everything, including the optional graph extra
uv run pytest -q                           # ~100 tests, SQLite, no services needed
uv run ruff check src tests && uv run ruff format --check src tests
RELAY_DATABASE_URL=sqlite+aiosqlite:///dev.db uv run alembic upgrade head   # migrations apply
RELAY_DATABASE_URL=sqlite+aiosqlite:///dev.db RELAY_ENVIRONMENT=test uv run relay serve   # API alone at :8000
```

Docker is not required for tests. CI runs the same commands plus the Postgres migration and a
`docker compose config` check; keep it green.

## How we work

- **Branch, never push to `main`.** `main` is protected: PRs only, CI must pass, linear history,
  one maintainer review. Name branches `feat/...`, `fix/...`, `docs/...`, `ci/...`.
- **Small conventional commits.** `feat(core): ...`, `fix(api): ...`, `docs: ...`, `test: ...`,
  `chore: ...`. Say why in the body when it isn't obvious.
- **Self-review before you open the PR.** Read your own diff as a reviewer would. If you are a
  coding agent, run a review pass (correctness, then a security lens if you touched auth, tokens,
  the broker, approvals, or uploads) and fix what it finds *before* submitting. A PR should arrive
  already reviewed once.
- **PR description says what and why**, links the ADR or issue, lists what you tested, and names
  anything you deliberately left out. Use the template.
- **Converge with the advisory reviewer before requesting a human.** outerloop reviews every PR
  opened from a branch in this repo (fork PRs are skipped) with `gpt-5.6-terra`, within minutes.
  Watching for that round and handling every finding is part of opening a PR, not an optional
  extra. Fix what is right, push, then remove and re-apply the `outerloop:review` label to get a
  fresh round on the new diff. Keep going until a round has no substantive findings, typically
  four or five rounds. On each inline finding, reply with the fixing commit and resolve the
  thread, or reply with a one-line reason and leave it open if you reject it. Silence a PR with
  `outerloop:no-review` only for trivial changes.
- **Rebase-merge** after CI is green and the maintainer approved. Do not merge your own PR.
- **Ask before big changes.** A new dependency, a schema change, a new service in compose, or
  anything touching the event schema deserves an issue or a short ADR first.
- **Keep the docs true.** `tests/test_docs.py` fails when the permissions table, event-type list,
  README tool table, or ADR index drift from code. Update them in the same PR.

## Editing safely (a lesson we paid for)

Prefer exact-match edits over scripted search-and-replace. A replace that finds nothing must fail
loudly, not silently no-op: an early PR shipped a dead feature because a formatter had changed the
anchor text. Rewrite a whole function or file rather than patching it blind. Run the tests after
every edit batch, not at the end.

## Where things live

```
src/relayagents/
  core/        events (schema), protocols, models (SQLAlchemy), store (EventStore), projections,
               approvals, permissions, redact, queue, migrations (Alembic)
  tools/       registry (the one definition) → mcp.py, rest.py, cli.py generators; handlers
  api/         FastAPI app factory, auth (tokens), routes/, a2a_broker/, slack/ (Socket Mode)
  workers/     extraction, pm (dispatch), digest, standup, jobs (arq), main
  ingest/      WhisperX transcriber, fixture transcriber, ingest worker
  connectors/  slack, github (gh CLI), coding_agents, hermes (provisioning), workspace, memory
  cli/         `relay` (typer) and the HTTP client
deploy/docker/ relay, ingest, hermes (bridge + skill), sandbox images
docs/          architecture, data-model, permissions, protocols, agent-contract, roadmap, adr/
tests/         one file per concern; conftest builds a three-user team on SQLite
fixtures/      synthetic transcript and audio only
```

## Adding things, by kind

- **Event type:** payload class in `core/events.py` and add it to `AnyPayload`; sample in
  `tests/test_events.py`; row in `docs/data-model.md`; projection branch if a read model changes;
  decide whether callers may publish it (`PUBLISHABLE_TYPES` in `api/routes/events.py`).
- **Tool:** `ToolSpec` in `tools/registry.py` with input/output models in `tools/schemas.py` and a
  handler in `tools/handlers.py`; row in the README table; `read_only` and `action_type` set
  honestly, because the audit trail and MCP annotations come from them.
- **Policy:** `core/permissions.py` and the table in `docs/permissions.md`.
- **Connector:** protocol first, then `connectors/<name>/`, wired in `api/app.py:build_services`
  behind a setting, with a fake for tests (see `RecordingChatApp`).
- **Setting:** `core/config.py`, then `.env.example` and, if a service needs it, `docker-compose.yml`.
- **Migration:** `src/relayagents/core/migrations/versions/`; must apply on SQLite and Postgres;
  think about existing data (see 0002 for the dedupe pattern).

## Things not to do

- Don't add `a2a-sdk` (its types are protobuf); the broker has its own wire models (ADR-0003).
- Don't put `OPENAI_API_KEY` or any model key into `relay-api`'s environment.
- Don't copy Slack channel history into the event log; Slack is a surface, events are the record
  (ADR-0009). Read Slack at request time if you must.
- Don't use `hermes chat -q` or assume Hermes speaks MCP; see `docs/roadmap.md` for the pending
  alignment and `deploy/docker/hermes/` for the bridge that avoids depending on Hermes internals.
- Don't pin to `main` of third-party repos in Dockerfiles once we have a tested version.
- Don't skip the migration when you change `core/models.py`.

## Reading order for a new contributor

`README.md` → `docs/architecture.md` → `docs/data-model.md` → `docs/permissions.md` →
`docs/adr/README.md` (0001, 0002, 0005, 0009 first) → `docs/roadmap.md`. Then run the tests and
read `tests/test_slices.py`: it walks both vertical slices end to end.
