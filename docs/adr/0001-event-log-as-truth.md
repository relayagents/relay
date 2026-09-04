# ADR-0001: The event log is the source of truth

**Status:** accepted · 2026-09-03

## Context

Relay holds meetings, decisions, action items, agent messages, approvals, and tool calls, and derives views from them: a knowledge graph, item/decision tables, digests, standups. Teams need to trust these views and to audit where any claim came from. Extraction is probabilistic and models change; we will want to re-derive.

## Decision

Every fact is an append-only `Event` in a Postgres table with a typed payload and provenance (segment ids, tool call ids, parent event ids). Projections (`action_items`, `decisions`), the Graphiti graph, and all summaries are derived and rebuildable with `relay replay`. Nothing is mutated in place; corrections are new events (`action_item.updated`, `decision.made` with `supersedes`).

## Alternatives

- **Mutable tables as truth, events as a side log.** Simpler queries, but the graph and dashboards would drift from the log and reprocessing with a better extractor would be a migration rather than a replay.
- **Graph as truth.** Graph databases are poor audit logs and the graph library is the part most likely to change (ADR-0005).

## Revisit if

Event volume makes replay impractical (we would add snapshots, not abandon the log), or a regulatory need arises to delete rather than supersede (we would add tombstone events and a compaction step).
