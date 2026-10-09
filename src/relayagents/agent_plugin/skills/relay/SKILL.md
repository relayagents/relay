---
name: relay
description: Use when the session has MCP tools from a server named relay (recall, my_items, report, request_approval, ask), when the user asks what the team decided or what is assigned to them, when finishing a piece of work, or when the user asks to tell the team something. Also use when a project's config names a relay MCP server that is not connected in this session, or when the user asks to connect a project to Relay.
---

# Relay

Relay is the team's shared memory: meetings become decisions and action items, and agents report
what they did so standups and digests write themselves. Everything sent to Relay is visible to the
whole team.

## Relay is on only where its tools are connected

A project opts in to Relay; the user's other projects do not. The signal is the session itself:
MCP tools from a server named `relay` (shown as `recall`, or with a prefix such as
`mcp__relay__report`).

- **Tools present:** use them as below.
- **Tools absent, and a file in this project turns Relay on** (`.claude/settings.json` or
  `.claude/settings.local.json` enables `relay@relayagents`, `.codex/config.toml` has
  `[mcp_servers.relay]`, `opencode.json` has `mcp.relay`, or `.cursor/mcp.json` has
  `mcpServers.relay`): Relay is meant to be on but is not connected. Do the user's task, then
  tell them once and offer to reconnect it (below). For Codex, OpenCode, or Cursor, check first,
  without printing it, that `RELAY_CODEX_TOKEN`, `RELAY_OPENCODE_TOKEN`, or `RELAY_CURSOR_TOKEN`
  is set; often that is all that is missing.
- **Tools absent otherwise:** this project is not on Relay. Do not mention Relay unless the user
  asks to connect it (below).

**Never use the `relay` shell command in place of the tools.** It is logged in as your human, not
as you, and it works in every folder, so it would publish as them and bypass the per-project
switch. If the tools are missing, the right result is "not sent" plus the offer to connect, even
when the user asked you to tell the team.

## Connecting a project

The one exception: when your human asks you to connect (or reconnect) this project, run
`relay setup-agent <claude-code|codex|opencode|cursor> --write` from the project root, following
https://github.com/relayagents/relay/blob/main/docs/connect-your-agent.md (logging in, storing
the token, what to tell your human). Their request is the opt-in; never connect a project on your
own initiative.

## Tools

| Tool | Use it to |
|---|---|
| `my_items` | see your human's open action items; keep the `id` of the one you are working on |
| `items` | see a teammate's or the team's items |
| `recall` | search team memory before answering about past work; cite the event ids it returns |
| `decisions` | get decisions on a topic, with what superseded what |
| `events` | see what happened recently (`since: "24h"`, `actor: "me"`) |
| `report` | publish a finished piece of work (below) |
| `ask` | put a question to a teammate's agent; the teammate is told in Slack |
| `request_approval` | get your human's approval in Slack for an external write (below) |
| `post` | post to the team channel as "<human>'s agent", only when your human asks |

## Report finished work

One `report` per finished piece of work: a fix landed, a PR opened, an item done, or a blocker the
team should know about. One or two sentences, with `link` and `item_id` when you have them;
`close_item: true` only when the item is done. Tell your human what you reported. Report only
work in this project, and never include secrets or private file contents.

## Approvals follow where the request came from

- **Your human asked for this action in this session** ("open a PR", "file an issue"): that is
  their approval. Do it; do not also send them to Slack.
- **The work came from Relay** (an action item, a teammate's request) **or your human is away**:
  call `request_approval` before the external write, with the matching `action_type`:
  `github.issue.create`, `github.issue.comment`, `github.pr.create`, `workspace.doc.write`,
  `workspace.calendar.write`, `slack.dm.other`, `coding_agent.run`. On `approved`, proceed (use
  `edited_action` if present); on `denied` or `expired`, stop and say so.

When you say the team decided or did something, cite the event id. If the record has nothing,
say so rather than guess.
