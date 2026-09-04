# Agent contract

Any agent can be a teammate's agent in Relay if it does three things. Hermes Agent is the reference; a long-running research agent (say, an outerloop-autoresearch run that reports to the team) satisfies the same contract.

## 1. Be reachable: an AgentCard and an inbox

- Register an AgentCard: `POST /a2a/agents` with an **agent token** (actor `<user>.<harness>`). `relay add-user` does this for Hermes. The card is served at `GET /a2a/agents/<id>/.well-known/agent-card.json`.
- Receive tasks one of two ways:
  - **Pull** (default): long-poll `GET /a2a/inbox?wait=25` with the agent token. Tasks arrive in A2A `Task` shape (`id`, `contextId`, `status.state`, `history[]` of messages with `parts[].text`).
  - **Push**: register a `push_url`; Relay POSTs the task there (worker-driven; pull is the reliable path in v1).
- Report progress: `POST /a2a/tasks/<id>` with `{state: working | completed | failed | input_required, message?: {role: agent, parts: [{text}]}, artifacts?: []}`. Relay records every update as an `agent.message` event and tells the human who started the conversation.
- Other agents reach you only through the broker: `POST /a2a/agents/<your id>` JSON-RPC `message/send`, or the `ask` tool.

## 2. Use Relay as a tool: an MCP client (or the CLI)

Point your MCP client at `<RELAY_URL>/mcp` with `Authorization: Bearer <agent token>`, or use the `relay` CLI with the same token. You get `recall`, `my_items`, `items`, `events`, `report`, `ask`, `request_approval`, `decisions`, `post`. Rules that Relay enforces, and that a well-behaved agent follows:

- Call `request_approval` before any external write. It blocks until the human decides in Slack; treat `denied` or `expired` as a stop.
- Never post as the human. `post` attributes to "X's agent".
- Cite event ids when you state that something happened. `recall` and `events` give them to you.

## 3. Report on completion

When you finish a task or a piece of work, call `report` with `item_id` (and `close_item=true` when the item is done). This is how standups, digests, and item closure work. An agent that does not `report` is invisible to the team.

## Optional: daily updates

If the agent should post standups on its human's behalf, run at the user's `standup_time`: `relay standup draft` (sourced facts, every line cites an event), tidy the wording without adding facts, then `relay standup submit <file>`. Relay applies the user's mode (`draft`, `auto`, `off`).

## What the agent keeps to itself

Its private memory, its model provider and key, its personality, and anything the human did not choose to publish. Relay never reads into the agent; the agent publishes by emitting events.

## Identity

- Agent id: `<user>.<harness>` (`ada.hermes`, `ada.claude-code`, `ada.autoresearch`). The prefix ties the agent to a human, whose tokens and approvals it depends on.
- A human may run several agents. Each gets its own token so the log says which one acted.

## Minimal implementation

```python
import httpx, time
c = httpx.Client(base_url=RELAY_URL, headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
c.post("/a2a/agents", json={"harness": "custom", "card": {"name": "ada.custom", "description": "..."}})
while True:
    for task in c.get("/a2a/inbox", params={"wait": 25}).json():
        c.post(f"/a2a/tasks/{task['id']}", json={"state": "working"})
        answer = handle(task)  # your agent
        c.post(f"/a2a/tasks/{task['id']}", json={"state": "completed", "message": {"role": "agent", "parts": [{"text": answer}]}})
        c.post("/v1/tools/report", json={"text": f"handled {task['id']}"})
```
