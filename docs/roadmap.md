# Roadmap

Where Relay is and what comes next, in order. Each item is small enough for one PR unless noted.
Update this file when you finish or reprioritize something; it is the handoff between contributors.

## Done (September 2026)

- Event schema, tool surface (MCP, CLI, REST from one registry), FastAPI API with per-user tokens,
  A2A broker, approvals in Slack, extraction (deterministic and LLM), PM dispatch, standups and
  digest, compose stack, docs and ADRs. Three self-review rounds; security hardening (PR #2).
- No graph by default; topic reuse and supersedes detection live in the extractor (ADR-0005).
- Positioning against Slackbot, Claude Tag, and Codex in Slack (ADR-0009). Claude Tag is not
  worth integrating for the lab right now; the laptop coding agent is the path.

## Next, in order

1. **Hermes alignment before the first boot.** The Hermes image assumes `hermes chat -q` and an
   MCP config block that Hermes does not have. Hermes is a pinned git clone driven as
   `uv run --project <repo> python run_agent.py --query=... --enabled_toolsets="file,terminal"`,
   with `~/.hermes/config.yaml` holding `model.default` / `model.provider` and `approvals.deny`.
   Update `deploy/docker/hermes/` (Dockerfile, entrypoint, bridge, config) and pin a tag.
   The `relay` CLI over the terminal tool is the integration; no MCP needed for Hermes.
2. **Cheaper or local models for the non-coding work.** Extraction, embeddings, standups, and
   digests do not need a frontier model. Both extraction and embeddings already accept an
   OpenAI-compatible endpoint via `OPENAI_BASE_URL` (vLLM, Ollama) or a cheap API tier. The one
   code change: make the embedding dimension configurable (currently fixed at 1536 in
   `core/models.py`) with a migration, since local embedding models are often 768 or 1024.
   Add `docs/models.md`: which model where, and how to point at a local server.
3. **First real boot** on a lab machine (Tailscale-only is fine while Google Workspace is deferred)
   or a small VPS. Run `scripts/bootstrap.sh`, upload `fixtures/transcript_sample.json`, watch the
   summary land in Slack. Expect the Hermes container and `workspace-mcp` env vars to need fixes.
   Then pin `HERMES_REF` and the sandbox npm CLIs to what worked, and tag `v0.1.0` so the release
   workflow publishes images.
4. **Team concept (ADR first).** A `team` is a Slack channel plus, optionally, a calendar; users
   belong to teams; events, meetings, and digests carry a nullable `team_id`; the per-team channel
   replaces the single `SLACK_TEAM_CHANNEL`. Setup must stay at three actions: invite the bot to a
   channel, share a calendar, add members. Slack is a surface, not a source of record (ADR-0009).
5. **Calendar-driven meetings.** A bot user with its own Google grant in `workspace-mcp` reads the
   team calendar, creates the meeting record ahead of time with attendees mapped by email, posts
   the link to the channel, and after the meeting pulls the transcript or recording into ingest.
   Creating a meeting link is a write and goes through the requester's grant and an approval.
6. **Vendor meeting notes as an ingest source.** Gemini Meet notes (and Slackbot recaps where a
   team has Business+) enter the same extraction path as a transcript.

## Known gaps, accepted for now

- The outerloop advisory reviewer (`.github/workflows/review.yml`) runs hermes on the OpenAI provider
  with `gpt-5.6-terra` and uses the `OPENAI_REVIEWER_KEY` repository secret (set 2026-09-09).

- REST does not check token scopes yet (MCP does).
- `ask.thread_id` is caller-chosen; `report --close-item` can close a teammate's item (attributed).
- The compose stack has never been run end to end; CI validates `docker compose config` and builds
  only the `relay` image. The ingest, Hermes, and sandbox images have never been built.
- Hermes and the sandbox's npm CLIs are unpinned until we have versions we tested.

## Parked

- Claude Tag connector (mechanics recorded in ADR-0009 if wanted).
- Discord `ChatApp` connector, if the lab moves; one connector behind the existing protocol.
- Knowledge graph (opt-in `graphiti-kuzu`; prefer FalkorDB Lite if ever enabled).

