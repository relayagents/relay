# Protocols

Every external system sits behind a `typing.Protocol` in `src/relayagents/core/protocols.py` with one reference implementation. Protocol first, then implementation.

| Protocol | Reference implementation | Alternatives that fit |
|---|---|---|
| `MemoryStore` — `index(events)`, `search(query)`, `reset()`, `close()` | `connectors/memory.GraphitiKuzuMemory` (Graphiti on embedded Kuzu) | Graphiti on FalkorDB Lite or Neo4j; a pure pgvector store; `NullMemory` |
| `Transcriber` — `transcribe(path, meeting_id, language, diarize)` | `ingest/whisperx_transcriber.WhisperXTranscriber` (+ pyannote) | faster-whisper without alignment; a hosted ASR; `FixtureTranscriber` for tests |
| `Extractor` — `extract(transcript, meeting_id, participants)` | `workers/extraction.LLMExtractor` (Pydantic AI structured output) | `KeywordExtractor` (deterministic, offline) |
| `OfficeSuite` — `search_documents`, `read_document`, `upcoming_meetings` | `connectors/workspace.WorkspaceMCP` (MCP client to `workspace-mcp`, per-user OAuth 2.1) | Microsoft 365 via a Graph MCP server (v2) |
| `ChatApp` — `post`, `dm`, `update` | `connectors/slack.SlackChatApp` (one app, Socket Mode) | Discord, Mattermost; `RecordingChatApp` for tests |
| `IssueTracker` — `create_issue`, `list_issues` | `connectors/github.GhIssueTracker` (`gh` with `GH_TOKEN` per user) | Linear, GitLab |
| `CodingAgent` — `run(prompt, workdir, env, timeout)` | `connectors/coding_agents.CliCodingAgent` for `claude -p`, `codex exec`, `opencode run`, local or in the sandbox image | any headless CLI |
| `UserAgent` — `provision(user, relay_url, relay_token)`, `deliver(task)` | `connectors/hermes.HermesUserAgent` (one container per user + `relay_bridge.py`) | anything satisfying [agent-contract.md](agent-contract.md) |

Model providers are behind Pydantic AI (extraction) and Graphiti's `LLMClient`/`EmbedderClient` (graph). Both read the team key from the workers' environment only.

## Transports (ADR-0003)

- **MCP** for SaaS tools that need per-user OAuth (`workspace-mcp`) and for Relay's own tool surface (`/mcp`, streamable HTTP, Relay-issued bearer tokens).
- **CLI** for local developer tooling (`gh`) and for invoking coding agents headlessly.
- **A2A** for agent-to-agent, always through Relay's broker (store-and-forward, `/a2a/*`), never peer-to-peer. Agents can live anywhere that can reach the node.

## Adding a connector

1. Extend the protocol (or add one) and document it here.
2. Add the implementation under `src/relayagents/connectors/<name>/`.
3. Wire it in `api/app.py:build_services` behind a setting.
4. Add an ADR if the choice is opinionated.
