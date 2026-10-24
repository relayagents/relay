# Roadmap

Where Relay is and what comes next, in order. Each item is small enough for one PR unless noted.
Update this file when you finish or reprioritize something; it is the handoff between contributors.

## Done (through October 2026)

- Event schema, tool surface (MCP, CLI, REST from one registry), FastAPI API with per-user tokens,
  A2A broker, approvals in Slack, extraction (deterministic and LLM), PM dispatch, standups and
  digest, compose stack, docs and ADRs. Three self-review rounds; security hardening (PR #2).
- No graph by default; topic reuse and supersedes detection live in the extractor (ADR-0005).
- Positioning against Slackbot, Claude Tag, and Codex in Slack (ADR-0009). Claude Tag is not
  worth integrating for the lab right now; the laptop coding agent is the path.
- October 2026: no per-user remote agent; Relay routes items and asks to people in Slack; standups
  scheduled by the workers; add-user mints the human token only (ADR-0011, supersedes ADR-0004).

## Next, in order

1. **Pull-request watcher** (ADR-0011, decision 4). Workers poll the lab's repositories with a
   read-only token, record `pr.*` events with the mapped person as actor, and post one quiet root
   message per PR to the team channel with threaded follow-ups that tag reviewers and authors.
   Merged PRs feed standups and the digest. Polling only; a webhook accelerator needs a public URL.
2. **Cheaper or local models for the non-coding work** (#25). Extraction and embeddings do not need
   a frontier model; standups and digests make no model calls. Both already accept an
   OpenAI-compatible endpoint via `OPENAI_BASE_URL` (vLLM, Ollama) or a cheap API tier. The one
   code change: make the embedding dimension configurable (currently fixed at 1536 in
   `core/models.py`) with a migration, since local embedding models are often 768 or 1024.
   Add `docs/models.md`: which model where, and how to point at a local server.
3. **First real boot** (#26) on a lab machine (Tailscale-only is fine while Google Workspace is
   deferred) or a small VPS. Run `scripts/bootstrap.sh`, upload `fixtures/transcript_sample.json`,
   watch the summary land in Slack, add one teammate and connect their coding agent in one project.
   Expect the `workspace-mcp` env vars to need fixes. Then tag `v0.1.0` so the release workflow
   publishes images.
4. **Team concept (ADR first)** (#27). A `team` is a Slack channel plus, optionally, a calendar;
   users belong to teams; events, meetings, and digests carry a nullable `team_id`; the per-team
   channel replaces the single `SLACK_TEAM_CHANNEL`. Setup must stay at three actions: invite the
   bot to a channel, share a calendar, add members. Slack is a surface, not a source of record
   (ADR-0009).
5. **Calendar-driven meetings** (#28). A bot user with its own Google grant in `workspace-mcp` reads
   the team calendar, creates the meeting record ahead of time with attendees mapped by email, posts
   the link to the channel, and after the meeting pulls the transcript or recording into ingest.
   Creating a meeting link is a write and goes through the requester's grant and an approval.
6. **Vendor meeting notes as an ingest source** (#29). Gemini Meet notes (and Slackbot recaps where
   a team has Business+) enter the same extraction path as a transcript.

## Known gaps, accepted for now

- The outerloop advisory reviewer (`.github/workflows/review.yml`) runs hermes on the OpenAI provider
  with `gpt-5.6-terra` and uses the `OPENAI_REVIEWER_KEY` repository secret (set 2026-09-09).
- `ask.thread_id` is caller-chosen (#31); `report --close-item` can close a teammate's item
  (attributed) (#32).
- The compose stack has never been run end to end; CI validates `docker compose config` and builds
  only the `relay` image. The ingest and sandbox images have never been built (#33).
- `relay-sandbox` has nothing that starts it since ADR-0011. Decide whether a future runner needs
  it or remove it.

## Deferred (ADR-0011 says why)

- **Notifications to agents.** An agent that is not running cannot be notified, and no
  agent-agnostic push exists yet. Pending asks reach the person in Slack and are answered in their
  next session. Revisit when a harness-agnostic path appears or a concrete workload waits on one.
- **Running or babysitting experiments.** An open question in ADR-0011: a Slurm cluster that needs
  an interactive 2FA login, often only over a VPN, with per-account quotas, cannot be reached from
  the node. One candidate is a per-person resident Slurm job that polls Relay outbound (option F in
  #40); the subagent launcher ADR decides.
- **GitHub webhook** as an accelerator for the PR watcher; needs a public URL (Tailscale Funnel, a
  tunnel, or a VPS).

## Parked

- Claude Tag connector (mechanics recorded in ADR-0009 if wanted).
- Discord `ChatApp` connector, if the lab moves; one connector behind the existing protocol.
- Knowledge graph (opt-in `graphiti-kuzu`; prefer FalkorDB Lite if ever enabled).

