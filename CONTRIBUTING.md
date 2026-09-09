# Contributing to Relay

Thanks for helping. Relay is small on purpose; keep it that way. `CLAUDE.md` is the short version
of everything below and is what coding agents read; humans should read it too.

## Setup

```bash
git clone https://github.com/relayagents/relay && cd relay
uv sync --extra graph
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
```

Tests run against SQLite and need no services. The Postgres migration and a compose config check
run in CI.

## The workflow

1. **Open or find an issue** for anything bigger than a typo, a test, or a doc fix. New dependency,
   schema change, new compose service, or event-schema change: write a short ADR in `docs/adr/`
   first (copy the shape of an existing one: context, decision, alternatives, revisit if).
2. **Branch from `main`**: `feat/...`, `fix/...`, `docs/...`, `ci/...`. Never push to `main`; it is
   protected (PRs only, CI required, linear history, maintainer review).
3. **Small conventional commits**: `feat(core): ...`, `fix(api): ...`, `docs: ...`, `test: ...`,
   `chore: ...`. If an AI coding agent wrote or co-wrote the change, keep the `Co-Authored-By`
   trailer it adds; we disclose that.
4. **Self-review before the PR.** Read your diff as a reviewer. If an agent helped, have it run a
   review pass (correctness; plus a security lens for auth, tokens, the broker, approvals, uploads)
   and fix findings first. Reviewers should be reading a diff that has already been reviewed once.
5. **Open the PR** with the template: what and why, how you tested, what you left out. CI must be
   green. An advisory AI review (outerloop) comments within a few minutes; it never approves or
   blocks, so treat it as a second reader, fix what is right, and say why when you disagree.
   Re-run it by applying the `outerloop:review` label; silence it on one PR with
   `outerloop:no-review`. A maintainer (see `.github/CODEOWNERS`) reviews; address comments with
   new commits, not force-pushes, until approved.
6. **Rebase-merge.** The maintainer merges, or tells you to. Don't merge your own PR.

## Rules that tests enforce

- Every event type has a sample in `tests/test_events.py` and a row in `docs/data-model.md`.
- The tool surface is defined once in `src/relayagents/tools/registry.py`;
  `tests/test_tool_surface.py` checks MCP, CLI, and REST agree, and the README table lists every tool.
- `src/relayagents/core/permissions.py` and the table in `docs/permissions.md` match.
- Every ADR file appears in `docs/adr/README.md`.

## Rules that people enforce

- **Protocol before implementation.** Extend `src/relayagents/core/protocols.py` and
  `docs/protocols.md`, then add the reference implementation under `src/relayagents/connectors/`.
- **Prefer boring, well-documented libraries.** If a stack choice looks wrong, open an issue with a
  one-paragraph rationale before building the replacement.
- **Fixtures are synthetic.** No real meetings, people, audio, transcripts, or Slack exports.
- **Never commit credentials.** `.env.example` only. Secret-scanning push protection is on.
- **Be kind in review.** Comment on the code, propose the fix, assume good faith. New contributors
  are learning the codebase and the conventions at the same time.

## Etiquette for AI-assisted work

Using Claude Code, Codex, or another agent on this repo is expected; the repo is built for teams
that do exactly that. Three habits keep it healthy:

- Point the agent at `CLAUDE.md` (Claude Code reads it automatically) and at the relevant ADR.
- Never paste tokens, `.env` contents, or real transcripts into an agent session.
- You own what you submit. Read the diff, run the tests, and be able to explain every change.

## Reporting security issues

Email the maintainers through the lab org rather than opening a public issue.
