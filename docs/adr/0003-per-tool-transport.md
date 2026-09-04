# ADR-0003: Per-tool transport: MCP, CLI, and A2A through the broker

**Status:** accepted · 2026-09-03

## Context

Three kinds of things need to talk: agents to SaaS tools with per-user OAuth (Google), agents to local developer tooling and headless coding agents, and agents to each other across machines.

## Decision

- **MCP** for SaaS tools that need per-user OAuth (`workspace-mcp`) and for Relay's own tool surface (streamable HTTP, Relay-issued bearer tokens). Every coding agent already speaks MCP.
- **CLI** for local developer tooling (`gh`) and for invoking coding agents headlessly (`claude -p`, `codex exec`, `opencode run`) in the sandbox. These tools are built for that and carry the user's own auth.
- **A2A** for agent-to-agent, always through Relay's store-and-forward broker, never peer-to-peer. Agents can live anywhere that reaches the node; nobody needs an inbound port; every exchange is an event and is surfaced to the affected human.

The broker implements the A2A wire shapes (AgentCard, Message, Task, JSON-RPC `message/send`, `tasks/get`) with small Pydantic models instead of depending on `a2a-sdk`, whose 1.x types are protobuf messages that fit poorly in a JSON-first service.

## Alternatives

- **MCP for everything.** MCP has no notion of a task that outlives a request or of one agent addressing another; we would reinvent A2A inside tool results.
- **Peer-to-peer A2A.** Requires every agent to be reachable and removes the single place where a human can watch.
- **Custom RPC.** Nobody else could plug in.

## Revisit if

A2A gains a standard brokered/relay mode (we would adopt its shapes), or `a2a-sdk` offers a light JSON types package.
