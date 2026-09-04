# ADR-0007: One Slack app via Socket Mode

**Status:** accepted · 2026-09-03

## Context

Humans watch and approve agent work in Slack. A lab node often sits behind Tailscale with no public inbound URL. Each teammate having their own Slack app is an onboarding tax.

## Decision

One Relay Slack app per workspace, connected over Socket Mode from `relay-api`. It posts summaries, digests, and attributed agent posts; DMs approvals, login requests, `ask` notifications, and standup drafts with Block Kit buttons; and maps Slack user ids to Relay users so only the right person can click Approve. No Events API URL is needed.

## Alternatives

- **Per-user Slack apps / user tokens.** Posts would look like the human wrote them; forbidden by policy (`slack.post.as_user`).
- **Events API with a public URL.** Requires exposing the node or a tunnel.
- **Discord/Mattermost first.** Fine later behind `ChatApp`; Slack is where the target teams are.

## Revisit if

Socket Mode limits (one connection per app token, rate limits) bite at team sizes Relay targets, which is unlikely below a few hundred users.
