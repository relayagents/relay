# Contributing to Relay

Thanks for helping. Relay is small on purpose; keep it that way.

## Setup

```bash
uv sync --extra graph
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
```

Tests run against SQLite and need no services. The Postgres migration is checked in CI.

## How we work

- **Small conventional commits.** `feat(core): ...`, `fix(api): ...`, `docs: ...`, `test: ...`, `chore: ...`.
- **Protocol before implementation.** A new connector starts by extending a protocol in `src/relayagents/core/protocols.py` and its section in `docs/protocols.md`, then adds the reference implementation under `src/relayagents/connectors/`.
- **Every event type gets a test.** Add a sample to `tests/test_events.py` when you add a payload, and a row to `docs/data-model.md`.
- **Tool surface changes touch one file.** Add a `ToolSpec` to `src/relayagents/tools/registry.py`; MCP, CLI, and REST follow. `tests/test_tool_surface.py` checks they agree.
- **Policy changes touch two files.** `src/relayagents/core/permissions.py` and `docs/permissions.md`; a test keeps them in sync.
- **Decisions get an ADR** in `docs/adr/` with alternatives and what would make us revisit.
- **Prefer boring, well-documented libraries.** If a stack choice looks wrong, open an issue with a one-paragraph rationale before building the replacement.

## Never commit

Real credentials, real meeting audio, real transcripts, or real Slack exports. `.env.example` only; fixtures are synthetic. Secret-scanning push protection is enabled on the repo.

## Reporting security issues

Email the maintainers through the lab org rather than opening a public issue.
