# ADR-0002: Relay is a substrate, not an agent framework

**Status:** accepted · 2026-09-03

## Context

Each teammate already runs an agent (Hermes, or something else) and a coding agent (Claude Code, Codex, OpenCode). Those tools have their own memory, model keys, and personalities. A "team agent platform" that replaced them would fight the tools people chose and would have to hold everyone's credentials.

## Decision

Relay defines the protocol surface (event schema, tool surface over MCP/CLI/REST, A2A broker, agent contract) and ships one working default for everything, but runs no agents of its own and holds no LLM keys for users. Private memory stays in the user's agent; Relay only sees what the agent publishes as events. Relay's PM job has no superuser credentials and asks the relevant person's agent instead. The only model key Relay holds is the team key its workers use for extraction and embeddings.

## Alternatives

- **Relay runs a team agent with shared credentials.** Faster demos, but violates least privilege, makes the audit log ambiguous about who acted, and forces one harness on everyone.
- **A pure library.** No shared memory or broker; every agent would need its own integration with every other.

## Revisit if

A team wants a Relay-operated agent for a role no human owns (an on-call triager). We would model it as a system user with its own scoped tokens and approvals rather than as Relay itself.
