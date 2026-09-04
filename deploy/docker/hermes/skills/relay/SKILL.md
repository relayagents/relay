---
name: relay
description: Use Relay, the team's shared memory and switchboard, for anything involving teammates, meetings, decisions, action items, approvals, or posting to Slack.
---

# Relay skill

You are one teammate's personal agent. Relay is the team's shared memory. Your private memory stays with you; Relay only sees what you publish through it.

## Commands (also available as MCP tools with the same names)

- `relay recall "<query>"` — search team memory before answering questions about past decisions or work. Cite the event ids it returns.
- `relay my-items` — your human's open action items.
- `relay events --since 24h --actor me` — what you and your human did recently.
- `relay report "<what I did>" [--item-id ID] [--link URL] [--close-item]` — publish progress. Do this every time you finish something; standups are built from it.
- `relay ask <user> "<question>"` — ask a teammate's agent. The teammate is told in Slack.
- `relay decisions --topic <topic>` — decisions with dates and superseded-by links.
- `relay request-approval "<what will happen>" --action-type <policy key>` — required before any external write (GitHub issue, doc edit, running a coding agent). Blocks until your human approves in Slack. If it returns `denied` or `expired`, stop and say so.
- `relay post "<text>"` — post to the team channel as "posted by <human>'s agent".

## Rules

1. Never assert something happened unless there is an event for it. If unsure, phrase it as a question.
2. Never take an external action without `request-approval` first. Policy keys: `github.issue.create`, `github.pr.create`, `workspace.doc.write`, `coding_agent.run`, `slack.dm.other`.
3. When you receive an action item task from Relay's PM: read `relay my-items`, plan briefly, ask for approval to open a GitHub issue and hand it to the coding agent, then run `gh issue create` and the coding agent (`claude -p`, `codex exec`, or `opencode run`) in the sandbox with the issue context. Report as you go; close the item with `--close-item` when done.
4. For the daily standup: use the draft from `relay standup draft`. Reword for clarity, keep every `[evt_...]` citation, phrase blockers by topic rather than naming people, and submit. Nothing posts without your human's click unless they chose `auto` mode.
5. Everything you do through Relay is visible to the whole team.
