# ADR-0008: Draft-by-default standups

**Status:** accepted · 2026-09-03

## Context

An agent posting a daily update on someone's behalf can misstate what they did, expose a blocker with a colleague's name in it, or simply be annoying. Trust in agent-written updates is earned.

## Decision

Standup mode is per user: `draft` (default), `auto`, `off`. In `draft`, the agent DMs a Block Kit draft; nothing posts without a click on Approve (or Edit, which opens a modal). In `auto`, the post carries "posted by X's agent" and an Edit button. Every post emits `approval.*` and `standup.posted`. The draft is built from events (`relay standup draft`): each line cites an event id, and anything the agent cannot source becomes a question in the draft rather than an assertion. Blockers that involve other people are phrased by topic, not by name, unless the human edits a name in. The team digest is posted by a Relay worker after the window and says "no update" on quiet days.

## Alternatives

- **Auto by default.** Faster adoption, higher chance of a wrong post on day one.
- **No agent standups.** Loses the main benefit of having `report` events.

## Revisit if

Teams consistently flip to `auto` after a week (then the default could follow), or drafts go unapproved (then a nudge or auto-expire makes sense).
