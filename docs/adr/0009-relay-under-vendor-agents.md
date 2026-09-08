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
  the user's permissions. Slack's revised terms prohibit apps from "indexing, copying, or
  permanently storing Slack messages" through the API.

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
4. **Slack content is read, never stored.** Relay posts to Slack and receives button clicks; it may
   query Slack at request time through the Real-time Search API or Slack's MCP server; it never
   copies channel history into the event log. A "team = Slack channel" design (ADR to follow) uses
   the channel for identity and delivery only.
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
  and OpenAI systems under their retention, and Slack's terms forbid keeping a structured copy of
  the conversation. No replay, no export, no reprocessing with a better extractor.
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

## Alternatives

- **Adopt Claude Tag alone and build nothing.** Cheapest in engineering time and already paid for;
  loses everything in the list above, including any meeting slice, and binds the lab to one vendor.
- **Build a Slack agent app that replaces the vendor agents.** Contradicts ADR-0002 and competes
  where the vendors are strongest.
- **Relay as a Slack-only product (no Hermes, no sandbox).** Simpler, but leaves out open-model
  agents and any team not on Slack. Kept as a possible profile, not the design.

## Revisit if

Slackbot's agent routing and memory become available on Pro or free plans with an exportable
record; Slack's terms change to allow structured storage of an app's own channel content; or a
vendor ships a neutral agent-to-agent broker with human approvals that open-model agents can join.
