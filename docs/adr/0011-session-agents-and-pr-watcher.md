# ADR-0011: Toward one hub with on-demand subagents: first, routing to humans and a pull-request watcher

**Status:** accepted · 2026-10-22 · supersedes [ADR-0004](0004-hermes-reference-agent.md) · amends [ADR-0002](0002-substrate-not-framework.md)

**Implementation:** this ADR records the decision; the code follows in two PRs. Decisions 2 to 4
(retire Hermes, route to humans, add-user) land first; decision 5 (the watcher) second. Until the
first lands, the Hermes image, connector, and `add-user.sh` container step still exist on `main`.

## Context

Relay's first design gave every teammate an always-on remote agent: one Hermes container per
person on the Relay node (ADR-0004). It never ran. The Hermes image assumes an invocation Hermes
does not have (#24), the compose stack has not booted end to end (#33), and each person would have
had two agent surfaces, the container and their laptop coding agent, whose context and behavior
drift apart. Meanwhile [ADR-0010](0010-coding-agents-opt-in-per-project.md) connects the laptop
coding agent to Relay per project, with its own token and a skill that says how to behave. That
covers "this person's agent" whenever they have a session open.

Issue #40 laid out the alternatives (one hub with session agents, a shared scheduler, a team agent
that acts, cluster-resident agents, vendor cloud agents, a hub that launches personal subagents).
The team settled on the last of these, option G: **one hub, plus human-oriented subagents launched
on demand**, for context isolation, never always-on. A subagent works from one person's
perspective: it carries out a task for them, browses the web, or watches a cluster job, then
reports back through Relay and exits. No subagent is resident, and none is shared between people.

G needs model calls, per-person model keys, and rules about who may launch whose subagent, none of
which exist yet. This ADR takes the first step toward it, a step that needs no model calls at all,
and leaves the hub and the launcher to a later ADR.

**What makes Relay worth running now** is an isolated place for project memory and team
communication, plus an organized daily check-in in Slack: each person's update, gathered and posted
as one team update. That check-in has to work for teammates whose coding agent is not connected or
not running, because nobody can ask someone else's agent to report. Worker-scheduled standups are
the core of that, and they are part of this first step.

Four constraints shape the step.

1. **Messages between people are for human judgment.** When a teammate's message reaches someone's
   agent, the agent must not try to carry it out: if the sender's agent could have done the work,
   the sender could have too. A subagent acts for the person who launched it, not for whoever
   messaged them.
2. **An agent that is not running cannot be notified.** Every scheme for pushing notifications to
   agents needs a process to receive them. Only Claude Code has session hooks; MCP server-to-client
   notifications are surfaced unevenly across harnesses; a Relay inbox that humans had to open
   would be one more channel to check. Until G's launcher exists, the reliable recipient of a
   message is the person, in Slack.
3. **Compute may sit behind an interactive login.** A typical case is a Slurm cluster that needs an
   interactive 2FA login, often only over a VPN, with per-account quotas. Nothing unattended on the
   Relay node can start or watch a job there, and one shared cluster account for the team does not
   fit per-account quotas. How a subagent reaches such a cluster is an open question below.
4. **The node may be reachable only over Tailscale at first boot** (#26). Nothing in this ADR may
   require an inbound public URL, for the same reason Slack runs in Socket Mode (ADR-0007).

## Decision

1. **Option G is the target shape; this ADR is its first step.** The first step makes no model
   calls on anyone's behalf: it routes work to humans, retires the per-user container, moves
   standups into the workers, and adds a pull-request watcher. Relay's PM and digest stay
   deterministic, and the only model calls on the node remain extraction and embeddings. **The hub
   and the subagent launcher come in a later ADR**, which must decide who may launch whose
   subagent, where model keys live and whose they are, and where personal memory lives.
2. **No always-on per-user agent.** The Hermes container, its image, the `connectors/hermes`
   provisioning code, the `UserAgent` protocol, and the container step in `scripts/add-user.sh` go
   away in the first implementation PR. The reference teammate agent is the laptop coding agent
   connected per project (ADR-0010). The A2A broker and `docs/agent-contract.md` stay for agents
   that register themselves (a custom agent, a long-running research run, and later G's
   subagents); Relay no longer provisions any.
3. **Relay routes to humans, and standups run in the workers.** PM dispatch will send the assignee
   a Slack DM with the item whether or not an agent is registered; today it marks the item
   unrouted. `ask` will record the question and DM the person instead of failing when they have no
   registered agent. A2A delivery still happens in addition when an agent exists. Standups move to
   the workers: a per-minute cron finds users whose `standup_time` has arrived in their timezone and
   runs the existing gather, render, and submit path in their mode, and each day's updates are
   threaded together as one team update in the channel. The draft is built from the event log, so
   it works with no agent connected; the agent rewording step that the Hermes bridge did is gone.
   ADR-0008 is unchanged.
4. **`add-user` mints the human token only** (today it also mints one for Hermes). Agents get their
   own tokens when the person runs `relay setup-agent` in a project. Minting an agent token at user
   creation would leave an unused secret on the node.
5. **A pull-request watcher, by polling, that writes events.** What only Relay adds is PR activity
   becoming events that feed standups and digests; GitHub's own Slack app already does per-PR
   threads, reviewer tags, and state updates. So the watcher ships polling and the `pr.*` events,
   and nothing in Slack.
   - A `CodeHost` protocol with one method, list the pull requests of a repository changed since a
     time, and one reference implementation on the GitHub REST API over `httpx`. The existing `gh`
     connector stays for writes under a user's token.
   - Workers poll each watched repository every few minutes. Pull requests are diffed against the
     `pull_requests` projection, and only state changes become events, so restarts and overlapping
     runs are idempotent. On first sight of a repository, already-open PRs get a backfilled
     `pr.opened`.
   - Five event types, all with `source="github"`: `pr.opened`, `pr.review_requested`,
     `pr.reviewed`, `pr.merged`, `pr.closed`. The **actor is the mapped Relay user** when the
     GitHub login is bound to one (`users.github_login`); `source` records that Relay observed the
     act on GitHub rather than the person acting in Relay. Unmapped logins fall back to the system
     actor `relay.github` with the login in the payload. Mapped actors are what make PRs appear in
     `events --actor` and in the person's standup draft with no further work, and in the digest's
     shipped section once the digest counts `pr.merged`. Draft PRs are ignored until they are
     marked ready for review, which is when `pr.opened` is recorded. Bot authors are skipped. A PR
     whose body or branch names a Relay item is linked to it (`item_id`); nothing closes an item
     automatically.
   - Configuration: a list of repositories, a poll interval, and a **read-only GitHub token given
     to `relay-workers` only**. This amends ADR-0002, under which the only credential Relay holds
     is the team model key in the workers and its PM has none of its own. The watcher adds a second
     team-level credential: a read-only one, for observing, never for writing. Writes to GitHub
     remain per user, under that user's own token, in their own agent.

## Open questions

- **How a subagent reaches a cluster behind interactive 2FA** (constraint 3). One candidate is a
  per-person resident Slurm job the person starts after one login, which polls Relay outbound and
  posts run-status events (option F in #40), running only experiments the person declared and using
  a model only to summarize results. Another is a machine the team owns on the tailnet, where a
  team token would be acceptable. The launcher ADR picks one or rules both out.
- **How a running agent hears about new work** (constraint 2). Considered: a "since your last call"
  block on every tool response (works for every MCP client, but only when the agent calls a Relay
  tool); MCP resource-update notifications (the only standards-based push, unevenly surfaced to the
  model); harness hooks (Claude Code only); Relay reading Slack for the agent (vendor agents in
  Slack already do this, ADR-0009). None is agent-agnostic and reliable yet, so pending `ask`s go
  to the person in Slack and are answered in their next session. A launcher may make this moot by
  starting a subagent instead of notifying one.

## Deferred, and why

- **Slack posts from the watcher** (one threaded message per PR, reviewer tags, editing the root on
  state change). GitHub's Slack app already does this; Relay adds it only if someone asks for it.
- **Webhook as an accelerator.** A signed GitHub webhook would only queue an immediate poll of
  that one repository; the poller stays the source of truth and the webhook body is never trusted
  as data. It needs a public URL (Tailscale Funnel, a tunnel, or a VPS), so it waits until someone
  needs instant delivery.
- **The sandbox image.** With no Hermes, nothing starts `relay-sandbox`. It is left in place and
  marked unused; whether G's subagents reuse it is for the launcher ADR.
- **Per-repository channels** follow the team concept (#27).

## Alternatives

- **Keep the per-user Hermes agent (option A).** An always-available delegate with private
  cross-project context. Rejected: never booted, duplicates the laptop agent, puts credentials on
  the node, and stays resident where G launches on demand and exits.
- **No hub at all: Relay stays a passive store for good.** Rejected as the end state: work that
  needs nobody's laptop open (watching a job, reading the web for someone) would have nowhere to
  run. It is, however, exactly the first step, so nothing built here is wasted.
- **Build G in one step.** Rejected: the hub needs answers on per-person keys, launch permissions,
  and personal memory that are not settled, and the near-term value (memory, routing, the daily
  check-in) needs none of them.
- **Webhook-first watcher.** Instant, but cannot be tested on a Tailscale-only first boot.
- **A GitHub App instead of a fine-grained token.** Better for many organizations and for
  webhooks; a read-only token scoped to the team's repositories is enough for one team.
- **Relay as a CI system that runs experiments on PRs.** Adjacent to CI, and the parts where Relay
  would add value (multi-hour Slurm jobs, provenance into team memory, per-person approval and
  budget, a written summary) all wait on the cluster open question.

## Revisit if

- The launcher ADR is written; it may change decisions 2 and 3 for teammates who opt in.
- A harness-agnostic way to notify a running agent appears.
- The cluster allows a resident per-user job, or the team gets its own compute on the tailnet.
- Someone asks for PR messages in Slack from Relay rather than from GitHub's app, or needs PR
  events faster than the poll interval (then a public URL and the webhook nudge).
- The team concept lands (#27).
