# ADR-0013: A meeting's decisions belong to the team; diarization is optional

**Status:** accepted · 2026-11-23

## Context

Relay transcribes meetings with WhisperX and labels who spoke with pyannote. pyannote's models
are gated on Hugging Face, so labeling speakers needs a Hugging Face token. Before this ADR the
ingest worker always asked for speaker labels, so a node without `RELAY_HF_TOKEN` failed every
meeting, although `.env.example` called the token optional.

Speaker labels do not name anyone. pyannote returns `SPEAKER_00`, `SPEAKER_01`, and so on, and
the ingest worker maps them to the meeting's participants in order of who spoke first, and only
when the counts match. That mapping is a guess. When it is wrong, the record says the wrong person
decided something, and nothing flags it.

The extractor used those labels as `decided_by`: the keyword extractor credited whoever said
"Decision: ...", and the model was told to fill `decided_by` with participant ids. A decision made
in a meeting is usually the group's, even when one person says it out loud.

## Decision

1. **Diarization is optional.** The ingest worker labels speakers only when `RELAY_HF_TOKEN` is
   set. Without it, every segment's speaker is `SPEAKER_UNKNOWN`, which is never mapped to a
   participant and is shown as `(unknown speaker)` in the extraction prompt, never as a name.
2. **A meeting's decision is a team decision.** The extractor no longer fills `decided_by`. The
   `decision.made` event already carries the meeting as `thread_id`. The `decisions` tool returns
   `meeting_id`, `meeting_title`, and an `attribution` string such as
   `Team decision in meeting "Retrieval sync"`, and returns no `decided_by` for a meeting's
   decision, including older events whose `decided_by` was guessed from speaker labels.
3. **`decided_by` stays in the event schema** for deciders a caller names when it publishes a
   decision through `POST /v1/events`. Old events still replay unchanged.
4. **Owners must be participants.** An extracted assignee or `asked_of` that is not one of the
   meeting's participants, such as a speaker label or an invented name, is dropped, so an item is
   never routed to someone who does not exist.

## Alternatives

- **Keep crediting the speaker.** Rejected: it depends on a guess that is silently wrong when the
  participant list is out of order, and credits one person with a group's decision.
- **Voice enrollment** (each member records a sample; match voices to people). It would make
  speaker labels real names, but it needs stored voice embeddings per user, consent, and a schema
  change. Not worth it while speaker labels only affect owners of "I'll do it" items.
- **A `meeting_id` column on the `decisions` projection.** Not needed: the source event already
  has it, and the `decisions` tool joins it in one query without a migration.
- **Drop `decided_by` from the event.** Payloads forbid extra fields, so old events would stop
  replaying.

## Revisit if

- Teams ask who proposed or agreed to a decision often enough to justify enrollment.
- Speaker labels become reliable names (a meeting platform that reports who is speaking), at which
  point "I'll do it" owners, not decisions, are where they help.
