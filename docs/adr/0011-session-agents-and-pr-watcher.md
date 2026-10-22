# ADR-0011: Session agents, routing to humans, and a pull-request watcher

**Status:** accepted · 2026-10-22 · supersedes [ADR-0004](0004-hermes-reference-agent.md) · amends [ADR-0002](0002-substrate-not-framework.md)

## Context

Relay's first design gave every teammate an always-on remote agent: one Hermes container per
person on the Relay node (ADR-0004). It never ran. The Hermes image assumes an invocation Hermes
does not have (#24), the compose stack has not booted end to end (#33), and each person would have
had two agent surfaces, the container and their laptop coding agent, whose context and behavior
drift apart. Meanwhile ADR-0010 (PR #39) connected the laptop coding agent to Relay per project,
with its own token and a skill that says how to behave. That covers "this person's agent"
whenever they have a session open.

Issue #40 laid out the alternatives (one hub with session agents, a shared scheduler, a team agent
that acts, cluster-resident agents, vendor cloud agents, a hub that launches personal subagents).
The lab's conclusion was a blend of "one hub" and "hub with subagents": no per-person remote
agents, one central node that holds the team's memory, and, later, subagents for long or
unattended work. Four constraints then cut that down to what this ADR decides.

1. **Messages between people are for human judgment.** When a teammate's message reaches someone's
   agent, the agent must not try to carry it out: if the sender's agent could have done the work,
   the sender could have too. Dropping "an agent executes a teammate's request" removes the need
   to run anything under another person's credentials, which was the hardest part of the
   cluster-resident and hub-with-subagents options.
2. **Experiments run on NYU's HPC cluster (Torch), and nothing on the Relay node can reach it.**
   Logging in needs an interactive 2FA step over the campus VPN, so no unattended process
   elsewhere can start or watch a job. Outbound HTTPS from the cluster works, so an agent could
   live there and talk out, but each Torch account has its own quota, so one shared cluster agent
   for the team is not workable either. Running or babysitting experiments is out of scope until
   one of those facts changes.
3. **An agent that is not running cannot be notified.** Every scheme for pushing notifications to
   agents needs a process to receive them. Only Claude Code has session hooks; MCP server-to-client
   notifications are surfaced unevenly across harnesses; a Relay inbox that humans had to open
   would be one more channel to check. The team wants an agent-agnostic answer, and there is none
   yet, so the messaging system is deferred rather than built badly.
4. **The node may be reachable only over Tailscale at first boot** (#26). Nothing in this ADR may
   require an inbound public URL, for the same reason Slack runs in Socket Mode (ADR-0007).

What is left is small and certain. GitHub is where the lab's work already flows, so watching pull
requests costs nothing to adopt, gives the team one concise Slack message per PR that tags the
right people, and feeds the standups and digests Relay already builds from events.

## Decision

1. **No per-user remote agent.** The Hermes container, its image, the `connectors/hermes`
   provisioning code, the `UserAgent` protocol, and the container step in `scripts/add-user.sh` are
   removed. The reference teammate agent is the laptop coding agent connected per project
   (ADR-0010). The A2A broker and `docs/agent-contract.md` stay for agents that register
   themselves (a custom agent, a long-running research run); Relay no longer provisions any.
2. **Relay routes to humans, not to agents.** PM dispatch sends the assignee a Slack DM with the
   item whether or not an agent is registered; today it marks the item unrouted. `ask` records the
   question and DMs the person, and no longer fails when they have no registered agent. A2A
   delivery still happens in addition when an agent exists. Standups are scheduled by the workers:
   a per-minute cron finds users whose `standup_time` has arrived in their timezone and runs the
   existing gather, render, and submit path in their mode. The draft is fully deterministic; the
   agent rewording step that the Hermes bridge did is gone. ADR-0008 is unchanged.
3. **`add-user` mints the human token only.** Agents get their own tokens when the person runs
   `relay setup-agent` in a project. Minting an agent token at user creation would leave an unused
   secret on the node.
4. **A pull-request watcher, by polling.**
   - A `CodeHost` protocol with one method, list the pull requests of a repository changed since a
     time, and one reference implementation on the GitHub REST API over `httpx`. The existing `gh`
     connector stays for writes under a user's token.
   - Workers poll each watched repository every few minutes. Pull requests are diffed against the
     `pull_requests` projection, and only state changes become events, so restarts and overlapping
     runs are idempotent. On first sight of a repository, already-open PRs get a backfilled
     `pr.opened` and no Slack post.
   - Five event types, all with `source="github"`: `pr.opened`, `pr.review_requested`,
     `pr.reviewed`, `pr.merged`, `pr.closed`. The **actor is the mapped Relay user** when the
     GitHub login is bound to one (`users.github_login`); `source` records that Relay observed the
     act on GitHub rather than the person acting in Relay. Unmapped logins fall back to the system
     actor `relay.github` with the login in the payload. Mapped actors are what make PRs appear in
     `events --actor`, in the person's standup draft, and in the digest's shipped section with no
     further work.
   - Slack stays quiet: one root message per PR in the team channel, everything after that as a
     thread reply, and the root edited on state change. Opened or ready-for-review tags the
     requested reviewers; a later review request tags that reviewer in the thread; a review tags
     the author with approved, changes requested, or commented; merged and closed post to the
     thread and tag nobody. Drafts are recorded but not posted until ready. Bot authors are
     skipped. A login with no mapped user is written as text, never tagged. A PR whose body or
     branch names a Relay item is linked to it (`item_id`) and the message says so; nothing closes
     an item automatically.
   - Configuration: a list of repositories, a poll interval, and a **read-only GitHub token given
     to `relay-workers` only**. This amends ADR-0002, which said Relay holds no GitHub token: Relay
     may hold a read-only credential for observing, never for writing. Writes to GitHub remain per
     user, under that user's own token, in their own agent.
5. **No hub agent.** "One central node" means Relay as it is: the event log, workers, broker, and
   Slack app. Relay's PM stays deterministic, and the only model calls on the node remain
   extraction, embeddings, and the digest. Nothing in this ADR adds a model call on a person's
   behalf.

## Deferred, and why

- **Notifications to agents (the messaging system).** Considered: a "since your last call" block
  on every tool response (works for every MCP client, including long sessions, but only when the
  agent calls a Relay tool); MCP resource-update notifications (the only standards-based push,
  unevenly surfaced to the model); harness hooks (Claude Code only); Relay reading Slack for the
  agent (vendor agents in Slack already do this, ADR-0009). Deferred because none is agent-agnostic
  and reliable, and because the human's inbox is Slack and must stay so. Pending `ask`s are therefore
  delivered to the person in Slack and answered in their next session.
- **Running or babysitting experiments.** Blocked by constraint 2. If resumed, the shape is a
  per-person resident Slurm job the person starts after one login, which polls Relay outbound and
  posts run-status events (option F in #40), running only experiments declared in the PR and using
  a model only to summarize results. A team-token runner would make sense only on lab-owned
  hardware on the tailnet. Neither is built here.
- **Webhook as an accelerator.** A signed GitHub webhook would only queue an immediate poll of
  that one repository; the poller stays the source of truth and the webhook body is never trusted
  as data. It needs a public URL (Tailscale Funnel, a tunnel, or a VPS), so it waits until someone
  needs instant delivery.
- **The sandbox image.** With no Hermes, nothing starts `relay-sandbox`. It is left in place and
  marked unused; removing it is a separate decision.
- **Per-repository channels** follow the team concept (#27). Until then the watcher posts to the
  single team channel.

## Alternatives

- **Keep the per-user Hermes agent (option A).** An always-available delegate with private
  cross-project context. Rejected: never booted, duplicates the laptop agent, puts credentials on
  the node, and the two workloads that justified it (acting while away, babysitting experiments)
  are blocked or deferred by the constraints above.
- **A hub agent that launches personal subagents (option G in full).** Rejected for now: it needs
  model keys brokered per person on the node, carries a context-confusion risk unless every
  request is a fresh session, and its workloads are the deferred ones. The event log, which G would
  have used as the hub's memory, is kept and is what the watcher writes into.
- **Webhook-first watcher.** Instant, but cannot be tested on a Tailscale-only first boot.
- **A GitHub App instead of a fine-grained token.** Better for many organizations and for
  webhooks; a read-only token scoped to the lab's repositories is enough for one lab.
- **Relay as a CI system that runs experiments on PRs.** Adjacent to CI, and the parts where Relay
  would add value (multi-hour Slurm jobs, provenance into team memory, per-person approval and
  budget, a written summary) all sit behind constraint 2.

## Revisit if

- A harness-agnostic way to notify a running agent appears, or the lab has a concrete workload
  that waits on one.
- NYU Torch or the lab allows a resident agent for a user, or the lab gets its own GPU machine on
  the tailnet.
- Someone needs PR notifications faster than the poll interval (then a public URL and the webhook
  nudge).
- The team concept lands (#27), which moves the watcher from one channel to one per team.
