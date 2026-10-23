# Architecture

Relay connects each person's coding agent to the team's meetings and chat. It is a switchboard and
a shared memory, not an agent. This page covers the components, the deployment topology, and the
two vertical slices. For what comes next, see [roadmap.md](roadmap.md).

## Components

| Service | Role | Image |
|---|---|---|
| `relay-api` | FastAPI: REST, the Relay MCP server (streamable HTTP, bearer tokens issued by Relay), the A2A broker, the Slack Socket Mode client | `ghcr.io/relayagents/relay` |
| `relay-workers` | arq on Redis: transcript extraction, PM dispatch, embeddings, standup scheduling, daily digest (graph indexing only when the opt-in graph is on) | same image, `workers` command |
| `relay-ingest` | arq on the `relay:ingest` queue: WhisperX + pyannote. CPU on the node, or GPU elsewhere | `ghcr.io/relayagents/relay-ingest` |
| `postgres` | Postgres 16 + pgvector. The `events` table is the source of truth | `pgvector/pgvector:pg16` |
| `redis` | Job queue | `redis:7` |
| `workspace-mcp` | Google Workspace MCP server with per-user OAuth 2.1 | `ghcr.io/taylorwilsdon/google_workspace_mcp` |
| `caddy` | TLS for `relay.relayagents.dev` or a tailnet name | `caddy:2` |
| `relay-sandbox` | headless coding-agent image; unused since ADR-0011 and kept until its future is decided | `ghcr.io/relayagents/relay-sandbox` |

Everything in `src/relayagents`:

```
core/        events (schema), protocols, models (SQLAlchemy), store (EventStore), projections, approvals, permissions
tools/       registry (the one definition) → mcp.py, rest.py, cli.py generators
api/         app factory, auth, routes/, a2a_broker/, slack/, mcp_server (via tools/mcp.py)
workers/     extraction, pm, digest, standup, jobs, main (arq)
ingest/      whisperx_transcriber, fixture, worker (arq, separate queue)
connectors/  slack, github (gh), coding_agents (CLI in sandbox), workspace (MCP client), memory (opt-in Graphiti graph, ADR-0005)
cli/         `relay` (typer) and the HTTP client
```

## Data flow

1. **Ingest.** `relay meeting upload` posts audio or a transcript JSON to `POST /v1/meetings`. Audio is queued on `relay:ingest`; the worker writes `transcript.json` and queues `extract_meeting` on relay-workers' `arq:queue`. Both jobs use a fixed id per meeting (`<job>:<meeting_id>`), so a duplicate enqueue is a no-op. Redis tickets are not the record: `requeue_stale_meetings` runs at worker startup and hourly, re-enqueueing every `queued` meeting, which re-drives any whose ticket was lost and leaves live ones alone. It never touches `transcribing`/`extracting`: a job is running, and that run finishes the meeting even if Redis loses its key. `extract_meeting` claims a meeting with a compare-and-set from `queued` (or from `extracting` on an arq retry), so a duplicate ticket backs off rather than dispatching twice.
2. **Extraction.** `extract_meeting` appends `transcript.segment` events, runs the extractor (Pydantic AI structured output, or the deterministic keyword extractor), and appends `decision.made`, `action_item.created`, `question.opened` events, each with `provenance.segment_ids`. Projections (`action_items`, `decisions`) update in the same transaction.
3. **PM.** The same job posts a summary to the team Slack channel and DMs each assignee their item. When the assignee has a registered agent, the item is also handed to it over A2A. Relay's PM has no credentials of its own and routes to people ([ADR-0011](adr/0011-session-agents-and-pr-watcher.md)).
4. **Agents.** Each teammate's coding agent reaches Relay over MCP from the projects that opted in ([ADR-0010](adr/0010-coding-agents-opt-in-per-project.md)): `my_items` shows what was assigned, `report` publishes what it did, `request_approval` gates external writes. Agents that want a push inbox register an AgentCard and long-poll `GET /a2a/inbox`; Relay provisions none itself.
5. **Memory.** Workers embed events with text (pgvector). `recall` runs the keyword leg in relay-api and asks the workers for the vector leg through the `semantic_recall` job, so the team model key never leaves the workers. Every hit carries the event's type, actor, thread, and linked ids (item, decision, supersedes, source segments), which is how an agent follows structure without a graph. The extractor reuses known topic names and marks which decision a new one supersedes, using the projections as its memory (ADR-0005). A knowledge graph (Graphiti) remains an opt-in `MemoryStore`.
6. **Daily updates.** A worker cron runs every minute and, at each user's `standup_time` in their timezone, builds the draft from events and applies their mode: `draft` DMs a Block Kit draft with Approve and Edit buttons; `auto` posts with the attribution "posted by Relay for X"; `off` does nothing. A run up to two hours late still sends it (a busy or restarting worker, or a time skipped by a DST jump), and each user gets at most one per local day; only the scheduler's own drafts and posts count toward that, so a draft approved after midnight does not cancel the next day's. Draft mode is skipped for a user with no Slack id, since nobody could be told. The workers do not hold the Socket Mode connection, so compose sets `RELAY_SLACK_BUTTONS` for them whenever relay-api has the app token: their drafts carry buttons, and relay-api's connection handles the clicks. A worker posts the team digest after the window.
7. **Replay.** `docker compose exec relay-workers relay replay --rebuild-graph --rebuild-projections` truncates derived stores (projections, embeddings, and the graph if enabled) and re-applies every event. It runs in the workers container because that is where the team key lives.

## Deployment topology

- **Relay node**: one VPS or lab machine (4 vCPU, 8–16 GB), joined to Tailscale. Runs everything in `docker-compose.yml`. Postgres and Redis bind to `RELAY_BIND_IP` (loopback by default; set it to the node's Tailscale IP to let a GPU worker in) and both require passwords generated by `scripts/bootstrap.sh`. Jobs are JSON-serialized, so a Redis write never becomes code execution in the workers. Only `relay-workers` receives the team model keys; `relay-ingest` gets database, queue, and transcription settings and nothing else.
- **GPU worker (optional)**: any machine on the tailnet with an NVIDIA GPU runs `docker-compose.gpu.yml` with `RELAY_REDIS_URL`/`RELAY_DATABASE_URL` pointing at the node. It consumes the same `relay:ingest` queue; stop the node's CPU `relay-ingest` to route everything to the GPU. Audio files must be reachable from both (share the `relaydata` volume via a synced folder; v1 does not stream audio between nodes).
- **Laptops**: the `relay` CLI, the Relay plugin or skill plus a per-project MCP config written by `relay setup-agent` (Relay is off in projects that did not opt in; [ADR-0010](adr/0010-coding-agents-opt-in-per-project.md)), and optional audio capture (Meetily, or any recorder plus `relay meeting upload`).
- **External**: one Slack app in Socket Mode (no inbound URL), Google via `workspace-mcp` with per-user OAuth, GitHub via `gh` with user tokens, model providers called directly by each agent. The only model key Relay itself holds is the team key used by workers for extraction and embeddings.

## Adding a teammate

`relay add-user` runs inside `relay-api` (it needs the database) and creates the user record and their human token; `scripts/add-user.sh <user>` wraps it from the node and prints the token once. Nothing else is provisioned: each person connects their own coding agent per project with `relay setup-agent`, which mints that agent's token ([ADR-0010](adr/0010-coding-agents-opt-in-per-project.md), [ADR-0011](adr/0011-session-agents-and-pr-watcher.md)).

## Auth

`relay login` runs a device flow: the CLI asks `POST /v1/auth/device`, Relay DMs the user in Slack with Approve/Deny buttons, the CLI polls and stores the token in `~/.config/relay/credentials.json`. Without Slack, an admin runs `relay admin approve-login <code>` on the node. Tokens are opaque, hashed with SHA-256 plus an optional pepper, and bound to one actor: the human (`ada`) or one of their agents (`ada.claude-code`, `ada.codex`). The MCP server verifies the same tokens as OAuth 2.1 bearer tokens.

## Vertical slice 1: meeting → action → code

Covered by `tests/test_extraction.py::test_extract_meeting_job_end_to_end`, `tests/test_a2a.py`, and `tests/test_approvals.py`. Run it for real with:

```bash
relay meeting upload --transcript fixtures/transcript_sample.json --skip-asr --participants ada,grace,linus
relay events --since 1h --thread <meeting id>
relay items --assignee grace
```

## Vertical slice 2: daily updates on behalf of each teammate

Covered by `tests/test_slices.py` and `tests/test_standup_schedule.py`. The workers' per-minute cron is the scheduler; `relay me --standup-mode auto|draft|off` sets the mode.
