"""Action policy: which external actions run automatically, which need a human, which never run.

The table here is the code form of docs/permissions.md. Keep them in sync (there is a test).
Policies are per action type; a deployment can override via ``RELAY_POLICY_OVERRIDES``
(JSON mapping) in a later version. Unknown action types default to ``approve``.
"""

from __future__ import annotations

from typing import Literal

Policy = Literal["auto", "approve", "forbid"]

DEFAULT_POLICY: dict[str, Policy] = {
    # Relay-internal (events only)
    "relay.report": "auto",
    "relay.ask": "auto",
    "relay.recall": "auto",
    # Chat
    "slack.dm.owner": "auto",  # an agent DMing its own human
    "slack.post.as_agent": "auto",  # attributed "posted by X's agent"
    "slack.post.as_user": "forbid",  # never impersonate a human
    "slack.dm.other": "approve",
    # GitHub
    "github.issue.create": "approve",
    "github.issue.comment": "approve",
    "github.pr.create": "approve",
    "github.push": "forbid",  # coding agents work on branches inside the sandbox; humans push
    # Google Workspace
    "workspace.doc.read": "auto",
    "workspace.doc.write": "approve",
    "workspace.calendar.write": "approve",
    "workspace.mail.send": "forbid",
    # Coding agents
    "coding_agent.run": "approve",
    # Standups
    "standup.post.draft": "auto",  # DM a draft to the owner
    "standup.post.auto": "auto",  # only when the user set mode=auto
}


def policy_for(action_type: str) -> Policy:
    return DEFAULT_POLICY.get(action_type, "approve")


def requires_approval(action_type: str) -> bool:
    return policy_for(action_type) == "approve"


def is_forbidden(action_type: str) -> bool:
    return policy_for(action_type) == "forbid"
