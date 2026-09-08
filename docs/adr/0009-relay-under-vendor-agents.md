# ADR-0009: Relay sits under the vendor agents, not beside them

**Status:** accepted · 2026-09-08

## Context

Between January and August 2026 the tools Relay's users already pay for grew agent features that
overlap two of Relay's slices:

- **Slackbot** (GA January 2026, Business+ and Enterprise+ only) transcribes and summarizes meetings
  with "action items assigned to them", is an MCP client, and can "route work or prompt questions
  to Agentforce or any agent or app in your enterprise".
- **Claude Tag** (replaced the Claude Slack app on 2026-08-03; Team, Enterprise, Pro, Max) keeps
  channel-scoped context over time, runs multi-step tasks through connected tools, has an ambient
  mode, and **Claude Code in Slack** starts a coding session from a thread.
- **Codex in Slack** (paid ChatGPT plans, connected GitHub) starts a cloud coding task from a
  mention and replies with a link.
- **Slack MCP server and Real-time Search API** (GA February 2026) let any agent query Slack under
  the user's permissions. Slack's API terms forbid *third-party* apps ("offered for use by others
  outside your organization") from keeping "persistent copies, archives, indexes, or long-term data
  stores of other organizations' API Data" or training an LLM on it. An app an organization runs
  for itself, on its own data, is not what those clauses address.

So "meeting → notes → items" and "ask a bot about Slack" are table stakes, and "mention a coding
agent in a thread" exists for the two big vendors. None of these products offers what a lab with
mixed agents needs: one memory that spans meetings, GitHub, and reports and is owned by the team;
a broker between agents that different people chose (a Hermes, a Claude, a Codex); approvals and
an audit trail that are the same regardless of vendor; standups built from work events rather than
chat; and any of it on a free or Pro Slack plan.

## Decision

1. **Positioning.** Relay is the substrate under the vendor agents: the event log, the approval and
   audit path, the broker, and the standup/digest source. It does not compete on meeting notes or
   chat Q&A. README and docs say so.
2. **Vendor agents are first-class executors.** Where a team has Claude Tag or Codex, Relay hands
   work to them by posting in a Slack thread and mentioning them, and they reach Relay's memory
   through its MCP server as a connector (`recall`, `my_items`, `report`, `request_approval`).
   For the lab this means Claude Tag and Claude Code in Slack on the Claude Team plan it already
   has; Claude Team supports custom connectors (remote MCP), which is how Relay's server is added
   (to verify at first boot). Relay's own per-user agent (Hermes) and sandbox remain the reference
   path for people without a vendor seat or using open models. The A2A broker stays for agents
   that can be addressed directly; Slack threads are the transport for vendor agents.
3. **Vendor meeting notes are an ingest source.** Slackbot recaps, Gemini Meet notes, and Zoom
   summaries enter the same extraction path as a transcript. WhisperX stays for recordings that
   must not leave the node and for teams without Business+.
4. **Slack is a surface, not a source of record.** Relay posts to Slack, receives button clicks,
   and may query Slack at request time through the Real-time Search API or Slack's MCP server. It
   does not copy channel history into the event log, for architectural reasons: Relay's events are
   its own record and Slack chatter is not one. This is a design choice, not a legal constraint: a
   self-hosted node is the organization's own app on its own data, and Slack's storage and
   LLM-training restrictions bind third-party apps handling other organizations' data. They would
   bind a hosted, multi-tenant Relay, which v1 is not. If channel history is ever wanted, the
   organization's official Slack export is the clean input. A "team = Slack channel" design (ADR to
   follow) uses the channel for identity and delivery.
5. **Own the plan gap.** Relay's first users are labs on Slack Pro or free with mixed providers.
   Every Relay feature must work without Business+, Claude Team, or ChatGPT Business.

## Price, from the lab's actual baseline (September 2026)

The lab is on Slack Pro or free, pays $15 per person per month for Claude Team, and does not plan
to buy Slack Business+ or ChatGPT Business. Coding-agent usage is paid for regardless of Relay, so
it is left out of the comparison.

| Option | Extra cost per person per month | What it adds |
|---|---|---|
| Baseline: Slack Pro/free + Claude Team | $0 (already paid) | Claude Tag with channel context and ambient mode, Claude Code in Slack, connectors |
| Add Slack Business+ | $15 (list) | Slackbot agent, Slack AI, huddle meeting notes |
| Add ChatGPT Business | $20 (list) | Codex in Slack |

So the realistic "adopt the vendors" stack for this lab is Claude Tag alone. It covers chat Q&A over
Slack, task execution through connectors, and coding from a thread. It does not cover meeting
notes (that is Slackbot, behind Business+, or Gemini in Google Meet where the Workspace plan
includes it), a record the lab owns, coordination with any non-Claude agent, standups from work
events, or approvals and audit across agents. Relay's direct cost is a small VPS (about $10 a
month); its real cost is the engineering and operations time to run it.

## What is lost by not building Relay

- **Ownership of the record.** Decisions, items, and approvals would live in Salesforce, Anthropic,
  and OpenAI systems under their retention; Slack's own export gives you raw messages, not the
  structured record. No replay, no reprocessing with a better extractor.
- **Coordination across vendors.** Slackbot, Claude Tag, and Codex do not talk to each other or to
  an open-model agent. A lab that mixes providers, or runs local models, has no shared substrate.
- **Uniform approvals and audit.** Each vendor agent has its own permission UI; nothing records
  "which agent did what, under whose token, approved by whom" across all of them.
- **Standups from what shipped.** Recaps summarize chat; nothing builds Done / Doing / Blocked from
  reports, closed items, and PRs with citations.
- **The research vehicle.** The lab studies agent coordination; vendor products cannot be
  instrumented or modified.
- **Access below Business+.** Slackbot's meeting notes and agent routing need Business+, which
  the lab is not buying; without Relay the meeting slice has no home at all.

## What is lost by building

Engineering weeks and ongoing operations; vendor polish on meeting notes and chat UX that Relay
should not try to match; and the risk that Slack later opens Slackbot's agent routing and memory to
lower plans, which would squeeze the broker half of Relay (the event log, approvals, and standups
would still stand).

## How Relay plugs into Claude Tag (verified against the Claude Tag docs, September 2026)

- **Mechanism.** Claude Tag runs each channel thread in an ephemeral cloud sandbox and reaches
  outside systems through an admin-managed *Access bundle*. A remote MCP server is added as a
  plugin whose `.mcp.json` points at the server URL, plus a **Bearer** credential whose allowed
  host is the server's hostname; the token is injected by Anthropic's egress proxy, so the sandbox
  never holds it. Relay's existing MCP server and opaque bearer tokens fit this without changes.
- **The node must be public.** Private and internal addresses are blocked by the proxy, so Relay's
  MCP endpoint must be reachable on the public internet with TLS (the VPS behind Caddy). A
  Tailscale-only node cannot serve Claude Tag.
- **Identity mismatch, by design on both sides.** In channels Claude Tag "acts under its own
  service accounts that an admin provisions, not as the person who asked", while Relay tokens are
  per person. So Claude Tag is one agent in Relay (a dedicated user owning it, e.g. `tag.claude`),
  its events say what thread and who asked in the payload text, and its approvals go to that
  owner. In DMs Claude Tag runs on the member's own claude.ai account with personal connectors, so
  a member can add Relay as a personal connector with their own token and be attributed correctly.
- **Laptops are separate.** Claude Tag has no link to a member's local Claude Code; the earlier
  personal "Claude Code in Slack" (Pro/Max) starts cloud sessions that can be pulled down with
  `--teleport`, but Tag sessions cannot. What ties a laptop Claude Code to the team is GitHub (its
  branches and PRs) and Relay's MCP server (`relay setup-agent claude-code`), so both the laptop
  agent and Claude Tag `report` into the same log.
- **Overlap to watch.** Claude Tag memory is curated notes per channel and workspace, editable by
  anyone in the channel and by an Owner, not an exportable structured record. Its routines can post
  a "daily standup summary" of open threads and a weekly digest of "what got decided, what's still
  open". That covers Slack-only signal; Relay's standups and digests differ by sourcing from work
  events (reports, closed items, PRs, approvals) with citations. If a team lives entirely in Slack,
  Tag's routines may be enough and Relay's slice 2 is optional for them.
- **Asking another person's agent for an update.** Claude Tag can answer "how's it going?" in the
  thread where it did the work, and can roll up open threads. It cannot query a coding agent
  running on someone's laptop; nothing vendor-side can. That is exactly Relay's `ask` and
  `events --actor`: the laptop agent reports as it works, and anyone can read or ask.
- **Cost.** Channel work draws from an organization usage balance with a spend limit, no per-seat
  charge; DMs bill to the member's own seat. There is a launch usage credit for Team and Enterprise.

## Alternatives

- **Adopt Claude Tag alone and build nothing.** Cheapest in engineering time and already paid for;
  loses everything in the list above, including any meeting slice, and binds the lab to one vendor.
- **Build a Slack agent app that replaces the vendor agents.** Contradicts ADR-0002 and competes
  where the vendors are strongest.
- **Relay as a Slack-only product (no Hermes, no sandbox).** Simpler, but leaves out open-model
  agents and any team not on Slack. Kept as a possible profile, not the design.

## Revisit if

Slackbot's agent routing and memory become available on Pro or free plans with an exportable
record; or a vendor ships a neutral agent-to-agent broker with human approvals that open-model
agents can join. If the lab moves to Discord, the `ChatApp` protocol is the seam: one connector,
no other change.
