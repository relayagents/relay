# Permissions

Relay's rule: **agents act under their human's own tokens, external writes need a click, and every external action is audit-logged.** Relay's PM function holds no credentials at all; it asks the relevant person's agent.

## Token model

| Token | Held by | Acts as | Obtained via |
|---|---|---|---|
| Relay human token | the person (CLI) | `human:<user>` | `relay login` (Slack-approved device flow) or `relay add-user` |
| Relay agent token | that person's agent container / coding agent | `agent:<user>.<harness>` | `relay add-user`, `relay setup-agent`, `POST /v1/tokens` |
| GitHub token | the person's agent environment (`GH_TOKEN`) | the person, via `gh` | the person creates it; Relay never stores it |
| Google OAuth grant | `workspace-mcp` per user | the person | OAuth consent in the browser; Relay never sees it |
| Team model key | workers only | Relay extraction/embeddings | `.env` (`TEAM_*_API_KEY`) |
| Slack tokens | `relay-api` | the Relay app | `.env` |

Tokens are opaque (`rly_...`), stored as SHA-256 with an optional pepper, expire after `RELAY_TOKEN_TTL_DAYS`, and can be revoked (`DELETE /v1/tokens/{id}`). Every event records the actor the token was bound to, so an agent can never be mistaken for its human in the log.

Rules that keep an agent from escalating to its human:

- Only a **human** token can mint tokens (`POST /v1/tokens`), revoke them, change identity bindings or posting mode (`PATCH /v1/me`), resolve approvals, or act as admin. An admin's agent token is not an admin.
- `POST /v1/users` refuses an existing user (409) unless `reissue=true`; re-issuing is logged as `token.issued` with `issued_via=admin`.
- The login device flow is approved only by the account owner in Slack or by an admin on the node; an unmapped Slack clicker is refused. One pending request per user per minute, and the DM asks the user to compare the code shown in their terminal.
- `slack_user_id` is unique across users, and changing it emits `user.updated`.
- Token minting, revocation, settings changes, and agent registration are all events (`token.issued`, `token.revoked`, `user.updated`, `agent.registered`).
- User ids `relay`, `system`, `admin`, and anything starting with `relay` are reserved for system actors.

## Default policy by action type

`auto` runs without asking. `approve` opens an approval the human resolves in Slack (Approve/Deny; blocks the caller). `forbid` is refused. Unknown action types default to `approve`. Source of truth in code: `src/relayagents/core/permissions.py` (a test keeps this table in sync).

| Action type | Default | Notes |
|---|---|---|
| `relay.report` | auto | events only |
| `relay.ask` | auto | events only; the asked human is notified |
| `relay.recall` | auto | read |
| `slack.dm.owner` | auto | an agent messaging its own human |
| `slack.post.as_agent` | auto | attributed "posted by X's agent"; channel ids only, never a user or DM id |
| `slack.post.as_user` | forbid | never impersonate a human |
| `slack.dm.other` | approve | |
| `github.issue.create` | approve | |
| `github.issue.comment` | approve | |
| `github.pr.create` | approve | |
| `github.push` | forbid | coding agents work on branches in the sandbox; humans push |
| `workspace.doc.read` | auto | under the user's own grant |
| `workspace.doc.write` | approve | |
| `workspace.calendar.write` | approve | |
| `workspace.mail.send` | forbid | |
| `coding_agent.run` | approve | headless run in the sandbox |
| `standup.post.draft` | auto | DMs a draft; posting still needs a click |
| `standup.post.auto` | auto | only when the user chose mode `auto` |

## Who can resolve an approval

Only the human it was requested of (`requested_of`), via the Slack buttons, `relay approvals approve|deny <id>`, or `POST /v1/approvals/{id}/resolve` with a human token. When the Slack app has a bot token but no Socket Mode app token, Relay sends notices without buttons and points to the CLI instead. Agents cannot resolve approvals, and other humans get a 403. The resolver may edit the action text; the edit is recorded in `approval.resolved.edited_action`.

## Audit log

Every state-changing tool call emits `tool.called` (arguments with secrets redacted recursively, including token-shaped strings, external `target`) and `tool.result` (ok/error, duration), linked by `call_id` and `provenance.parent_event_ids`. Approval `details` are redacted the same way before they are stored. Approvals emit `approval.requested`/`approval.resolved`. A2A exchanges emit `agent.message` with `surfaced_to`, the humans who were notified. Read-only tools are not audited by default; set `RELAY_AUDIT_READ_TOOLS=1` to log them too. `relay events --include-tool-calls` shows the audit trail.

## Surfacing

Anything that would surprise a human later is posted to that human (principle 7): an `ask` DMs the asked person, a completed or failed task DMs the asker, a PM hand-off DMs the assignee, approvals DM the approver, and standups and digests go to the team channel with attribution.
