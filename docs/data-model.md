# Data model

## The event

Every fact in Relay is an append-only `Event` (`src/relayagents/core/events.py`):

| Field | Type | Notes |
|---|---|---|
| `id` | `evt_<ULID>` | time-sortable |
| `ts` | datetime (UTC) | when it happened |
| `type` | string | equals `payload.type`; on the envelope so the log can be filtered without parsing |
| `actor` | `{kind: human \| agent \| system, id}` | agents are `<user>.<harness>`; `Actor.user_id` gives the owning human |
| `source` | `meeting \| slack \| workspace \| github \| a2a \| cli \| api` | where it entered Relay |
| `visibility` | `team \| public` | Relay has no private scope by design |
| `thread_id` | string or null | meeting id, A2A context id, approval id, item id, `standup:<user>:<date>`, `digest:<date>` |
| `payload` | typed, discriminated on `type` | extra fields are rejected |
| `provenance` | `{segment_ids, tool_call_ids, parent_event_ids}` | how to audit where this came from |

## Event types

| Type | Payload (key fields) | Emitted by |
|---|---|---|
| `meeting.started` | `meeting_id, title, participants, started_at, recording_ref` | upload route |
| `transcript.segment` | `meeting_id, segment_id, speaker, start_s, end_s, text, confidence` | ingest / extract job |
| `decision.made` | `decision_id, statement, topic, rationale, decided_by, supersedes` | extractor |
| `action_item.created` | `item_id, title, assignee, due, details, meeting_id` | extractor, `POST /v1/events` |
| `action_item.updated` | `item_id, title?, assignee?, due?, status? (open/in_progress/blocked), note?` | agents |
| `action_item.closed` | `item_id, resolution (done/wont_do/duplicate), note, links` | `report --close-item` |
| `question.opened` | `question_id, text, asked_of, context` | extractor, `ask` |
| `question.answered` | `question_id, answer` | agents |
| `report.posted` | `text, item_id, links` | `report` (the standup source) |
| `tool.called` | `call_id, tool, transport, arguments (redacted), target` | tool runtime, for state-changing tools |
| `tool.result` | `call_id, tool, ok, summary, error, duration_ms` | tool runtime |
| `agent.message` | `task_id, from_agent, to_agent, role, text, state, surfaced_to` | A2A broker |
| `approval.requested` | `approval_id, action, action_type, requested_of, details, expires_at` | `request_approval`, standup draft |
| `approval.resolved` | `approval_id, decision (approved/denied/expired), resolved_by, edited_action, note` | Slack buttons, REST |
| `standup.posted` | `user_id, mode, done, doing, blocked, questions, cited_event_ids, channel, message_ref` | standup submit |
| `digest.posted` | `window_start, window_end, shipped, in_progress, blockers, decisions_needed, cited_event_ids, quiet` | digest worker |
| `token.issued` | `token_id, user_id, token_actor, label, expires_at, issued_via (add_user/device_flow/api/admin)` | token minting |
| `token.revoked` | `token_id, user_id, reason` | `DELETE /v1/tokens/{id}` |
| `user.updated` | `user_id, changes` | `PATCH /v1/me` (identity bindings, posting mode) |
| `agent.registered` | `agent_id, user_id, harness, push_url` | AgentCard registration |

Callers may publish only `report.posted`, `question.*`, `action_item.*`, and `decision.made` through `POST /v1/events`; every other type is produced by Relay itself (ingest, extraction, broker, approvals, standups, digests, tokens). `source` is set by the server.

Adding a type: add the payload class and include it in `AnyPayload`, add a sample to `tests/test_events.py`, add a row here, and (if projections care) a branch in `core/projections.py`.

## Tables

| Table | Kind | Notes |
|---|---|---|
| `events` | **truth** | `seq` bigint cursor, `id` unique, JSON `payload`/`provenance`, `text_index` for lexical search, `embedding vector(1536)` (pgvector; JSON on SQLite), GIN full-text index on Postgres |
| `action_items`, `decisions` | projection | rebuilt by `relay replay --rebuild-projections` |
| Graphiti graph (Kuzu file) | projection | rebuilt by `relay replay --rebuild-graph` |
| `users` | operational | identity, Slack/GitHub ids, timezone, `standup_mode`, `standup_time` |
| `api_tokens` | operational | hashed tokens bound to an actor; scopes; expiry; revocation |
| `device_codes` | operational | `relay login` device flow |
| `agents` | operational | A2A AgentCard registry, optional `push_url` |
| `a2a_tasks` | operational | store-and-forward inbox/outbox; messages are also events |
| `approvals` | operational | pending/approved/denied/expired, Slack message ref for updates |
| `meetings` | operational | status machine `queued → transcribing → extracting → done | failed`, file paths |

Migrations live in `src/relayagents/core/migrations` (Alembic, async). `relay migrate` applies them; the API container does so on start.

## Search

`recall` runs three legs and merges by score, deduplicated by event id: lexical search over `text_index` (in relay-api), and, through the `semantic_recall` job in relay-workers, pgvector cosine search over event embeddings plus Graphiti graph search (facts with validity intervals and episode ids). The semantic legs live in the workers because they need the team model key, which relay-api never holds. Each hit carries `event_ids` so an agent can cite it. Embeddings are written when events are indexed and rebuilt by `relay replay --rebuild-graph`.
