# Relay

**Relay is the shared memory and switchboard for a team whose members each run their own AI agent.** It turns meetings into structured decisions and action items, keeps a team knowledge graph, exposes all of it to any agent as tools, and brokers agent-to-agent messages with a human watching. Every teammate keeps the agent and coding agent they already use; Relay is the protocol surface they plug into.

Relay is **not** an agent framework. It runs no model calls on your behalf, holds no private memory, and asks your agent for anything that needs your credentials.

## 60-second quickstart

```bash
git clone https://github.com/relayagents/relay && cd relay
./scripts/bootstrap.sh            # writes .env with generated secrets
$EDITOR .env                      # hostname, Slack tokens, team model key (optional)
./scripts/bootstrap.sh            # docker compose up -d --build, waits for /health, creates the admin
uv tool install git+https://github.com/relayagents/relay   # the `relay` CLI on your laptop (PyPI release pending)
relay login --url https://relay.example.dev --token <printed above>
scripts/add-user.sh grace         # on the node: user, tokens, AgentCard, and a Hermes container
relay setup-agent claude-code     # points your coding agent at Relay's MCP server
relay meeting upload --transcript fixtures/transcript_sample.json --skip-asr --participants ada,grace,linus
relay my-items                    # ...and `relay recall "embedding cache"`, `relay decisions`
```

No Docker on hand? `RELAY_DATABASE_URL=sqlite+aiosqlite:///relay.db RELAY_ENVIRONMENT=test uv run relay serve` runs the API alone for a look around.

## How it fits together

```mermaid
flowchart LR
  subgraph laptops["Laptops"]
    CLI["relay CLI"]
    CC["Coding agent<br/>(Claude Code / Codex / OpenCode)"]
  end
  subgraph node["Relay node (VPS or Mac mini, on Tailscale)"]
    API["relay-api<br/>REST · MCP server · A2A broker · Slack Socket Mode"]
    W["relay-workers<br/>extraction · PM · digest · graph"]
    PG[("Postgres + pgvector<br/>event log = truth")]
    R[("Redis / arq")]
    G[("Graphiti on Kuzu<br/>derived graph")]
    WS["workspace-mcp<br/>per-user Google OAuth"]
    subgraph pool["Agent pool"]
      H1["Hermes · ada"]
      H2["Hermes · grace"]
      SB["sandbox: claude -p / codex exec"]
    end
    CADDY["caddy (TLS)"]
  end
  GPU["relay-ingest (WhisperX)<br/>GPU box on the tailnet, or CPU fallback"]
  SLACK["Slack (one app)"]
  GH["GitHub (gh, user tokens)"]

  CLI -->|bearer token| CADDY --> API
  CC -->|MCP| CADDY
  API <--> PG
  API <--> R
  W <--> PG
  W <--> R
  W --> G
  GPU <--> R
  API <-->|A2A via broker| H1
  API <-->|A2A via broker| H2
  H1 -->|headless run| SB
  SB -->|MCP: my_items, report| API
  API <-->|Socket Mode| SLACK
  H1 -->|gh, user token| GH
  H1 -->|MCP| WS
```

Read [docs/architecture.md](docs/architecture.md) for the deployment topology, [docs/data-model.md](docs/data-model.md) for the event schema, [docs/permissions.md](docs/permissions.md) for what agents may do without asking, [docs/protocols.md](docs/protocols.md) for the pluggable boundaries, and [docs/agent-contract.md](docs/agent-contract.md) if you want to plug in an agent that is not Hermes. Design decisions live in [docs/adr](docs/adr).

## The tool surface

The same nine operations exist as MCP tools, `relay` subcommands, and `POST /v1/tools/<name>`, generated from one definition ([src/relayagents/tools/registry.py](src/relayagents/tools/registry.py)) so they cannot drift.

| Operation | Purpose |
|---|---|
| `recall <query>` | hybrid search over team memory (graph + pgvector + event log) with provenance |
| `my_items` / `items --assignee` | open action items, with source meeting and status |
| `events --since --type --thread` | query the event log |
| `report <text> [--item-id ID] [--link URL]` | publish what I did as an event (source for standups and item closure) |
| `ask <user> <question>` | A2A message to a teammate's agent, threaded, surfaced to that human in Slack |
| `request_approval <action>` | open an approval; blocks until the human resolves it in Slack |
| `decisions --topic` | decisions with dates and superseded-by links |
| `post <text>` | post to Slack via Relay's app with "posted by X's agent" attribution |

## Principles

1. **The event log is the source of truth.** Graph, projections, digests: all derived, all rebuildable with `relay replay`.
2. **Relay holds team memory only.** Private memory stays in your agent. Relay stores no LLM keys and proxies no model traffic.
3. **Delegated, per-user permission.** Agents act under their human's tokens. External writes are approval-gated by default and audit-logged always.
4. **Per-tool transport.** MCP for SaaS tools and Relay's own surface, CLI for local tooling and headless coding agents, A2A through Relay's broker for agent-to-agent.
5. **Pluggable everything**, one reference implementation each.
6. **One compose file runs it.**
7. **Anything that would surprise a human later is surfaced to that human.**
8. **Public from commit one.** Synthetic fixtures only.

## Deliberately not in v1

- Live meeting bots (v1 accepts recordings and transcripts; live capture is v2).
- Microsoft 365 (the `OfficeSuite` protocol is there; Google Workspace is the reference).
- Multi-tenant hosting. One node is one team.
- Running Relay's own agents. Relay's PM function has no credentials and asks your agent instead.

## Status

Pre-alpha. The two vertical slices (meeting → action → code, and daily updates on behalf of each teammate) are wired end to end and covered by tests against SQLite; the compose stack targets Postgres + pgvector. Expect the Hermes container config keys to need adjustment for the Hermes version you pin.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Apache-2.0.

---

From the [Agentic Learning AI Lab](https://github.com/agentic-learning-ai-lab).
