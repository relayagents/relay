# ADR-0004: Hermes Agent as the reference per-user agent

**Status:** accepted · 2026-09-03

## Context

Relay needs one per-user agent harness that works out of the box so `relay add-user` can provision a teammate end to end. It must be open source, self-hostable, able to call MCP tools, run shell commands (`relay`, `gh`), and be driven headlessly.

## Decision

Hermes Agent (Nous Research) is the reference. `relay add-user` starts one `relay-hermes-<user>` container per teammate with its own volume, seeded with a `relay` skill and Relay's MCP server pre-configured. A small bridge (`deploy/docker/hermes/relay_bridge.py`) long-polls the A2A inbox and runs Hermes headlessly per task, and runs the daily standup at the user's time. The bridge keeps Relay's dependency on Hermes to "run a prompt from the CLI"; any harness that speaks A2A and MCP can replace it (see `docs/agent-contract.md`).

## Alternatives

- **Claude Code / Codex as the user agent.** Excellent coding agents, weak as always-on personal agents with cron and messaging; they remain the coding agents Hermes hands work to.
- **Relay's own minimal agent.** Contradicts ADR-0002 and would be worse than any maintained harness.
- **No default.** `add-user` would provision tokens only; the first-run experience would be a research project.

## Revisit if

Hermes changes its config or CLI in ways the bridge cannot paper over, or a harness with native A2A inbox support becomes common. The Hermes-specific config keys in `deploy/docker/hermes/config.yaml` are marked "verify" and pinned by `HERMES_REF`.
