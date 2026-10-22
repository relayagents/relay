# Contributing to Relay

Thanks for helping. Relay is small on purpose; keep it that way. `CLAUDE.md` is the short version
of everything below and is what coding agents read; humans should read it too.

## Setup

You don't need write access to contribute. Fork the repo and work in your fork; the
[GitHub CLI](https://cli.github.com) does the fork, the clone, and an `upstream` remote in one step
(or fork on github.com, clone your fork, and `git remote add upstream https://github.com/relayagents/relay`).
Maintainers with write access can clone `relayagents/relay` directly and push branches to it.

```bash
gh repo fork relayagents/relay --clone && cd relay
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
   protected (PRs only, CI required, linear history, maintainer review). In a fork, start from the
   latest upstream: `git fetch upstream && git switch -c docs/my-fix upstream/main`.
3. **Small conventional commits**: `feat(core): ...`, `fix(api): ...`, `docs: ...`, `test: ...`,
   `chore: ...`. If an AI coding agent wrote or co-wrote the change, keep the `Co-Authored-By`
   trailer it adds; we disclose that.
4. **Self-review before the PR.** Read your diff as a reviewer. If an agent helped, have it run a
   review pass (correctness; plus a security lens for auth, tokens, the broker, approvals, uploads)
   and fix findings first. Reviewers should be reading a diff that has already been reviewed once.
5. **Open the PR** with the template: what and why, how you tested, what you left out. CI must be
   green. An advisory AI review (outerloop) comments within a few minutes; it never approves or
   blocks, so treat it as a second reader, fix what is right, and say why when you disagree.
   Silence it on one PR with `outerloop:no-review`.
6. **Converge with the reviewer before asking a human.** Opening the PR starts round 1; watch for
   it (a few minutes) and handle every finding before doing anything else. Each round reviews the
   diff as it is *now*, so after you push fixes, remove the `outerloop:review` label and apply it
   again to get a fresh round on the new commits (the workflow fires on the label being applied). Repeat until
   a round comes back with nothing substantive. That usually takes four or five rounds on a
   real change; fewer means the change was small, many more means the change wants splitting.
   On each inline finding, reply with the commit that fixed it and resolve the thread, or reply
   with a one-line reason if you reject it and leave it open, so the next round and the maintainer
   see the state at a glance. Only then request the maintainer's review.
7. A maintainer (see `.github/CODEOWNERS`) reviews; address comments with new commits, not
   force-pushes, until approved.
8. **Rebase-merge.** Once a maintainer other than you has approved and CI is green, you (or the
   maintainer) rebase-merge. If `main` moved, click "Update branch" and let CI rerun first.

### Pull requests from a fork

The same workflow, with four differences:

- **CI may wait for a maintainer.** GitHub holds workflow runs on a first-time contributor's PR
  until a maintainer approves them, so CI may show as pending at first. That's expected.
- **No outerloop review.** The advisory reviewer skips fork PRs because it would expose its model
  key to code from outside the repo, and applying labels needs repository access. Steps 5 and 6
  don't apply, so the maintainer's review is the first one; the self-review in step 4 matters more.
  Say in the PR that it came from a fork. For a large change, a maintainer may push your branch
  into the repo to run the advisory reviewer on it.
- **Leave "Allow edits by maintainers" ticked** when you open the PR, so a maintainer can update
  your branch when `main` moves or push a small fix.
- **A maintainer merges.** You can't merge into this repo, so a maintainer rebase-merges once the
  PR is approved and CI is green.

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
