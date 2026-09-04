# ADR-0005: Graphiti on embedded Kuzu for the team graph

**Status:** accepted with a known risk · 2026-09-03

## Context

`recall` should answer "what did we decide about X and when" with temporal validity, not just nearest neighbours. Graphiti builds a temporal knowledge graph from episodes and handles fact invalidation; it needs a graph store. Relay must run on a €10 VPS with one compose file, so the store should be embedded.

## Decision

Graphiti with its Kuzu driver, database file in the workers volume, behind the `MemoryStore` protocol. The graph is derived: `relay replay --rebuild-graph` wipes and re-indexes from the event log. Event-log keyword search and pgvector remain independent legs of `recall`, so a broken graph degrades rather than breaks recall.

## Known risk

Kuzu's upstream development stopped in October 2025 (last PyPI release 0.11.3). The package still installs and Graphiti still ships the driver, so it works today, but it will not get fixes. Graphiti also ships an embedded FalkorDB Lite driver (Python 3.12+), which is the planned replacement: swapping is a change in `connectors/memory` and a config value, plus a replay.

## Alternatives

- **FalkorDB Lite (embedded).** Actively maintained, supported by Graphiti. Newer and less battle-tested in Graphiti than Kuzu was when this was written; first candidate for the swap.
- **Neo4j.** Another service in compose and more memory than the target box.
- **pgvector only.** No temporal facts or entity resolution; kept as a leg of hybrid search.

## Revisit if

Kuzu fails to install on a supported Python, Graphiti drops the driver, or FalkorDB Lite proves stable in a Relay deployment. Any of these flips the default to FalkorDB Lite.
