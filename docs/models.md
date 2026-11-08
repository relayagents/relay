# Models: which one where, and running them locally

Relay itself calls a model for two jobs: pulling decisions and action items out of meetings, and
embedding events for `recall`. Both run in `relay-workers`, under the team's model settings.
relay-api never calls a model and never sees a model key ([ADR-0002](adr/0002-substrate-not-framework.md)).
Each teammate's own coding agent uses its own model and key; that is outside Relay.

Neither job needs a frontier model, and both can run on a model server you host.

## Which model where

| Job | Setting | Default | What the model must do |
|---|---|---|---|
| Meeting extraction | `RELAY_EXTRACTION_MODEL` | `keyword`: a deterministic extractor that runs offline, with no model and no key | Chat model with **tool calling** (see below) |
| Embeddings, for the vector leg of `recall` | `RELAY_EMBEDDING_MODEL` and `RELAY_EMBEDDING_DIM` | `openai:text-embedding-3-small`, 1536 | Embedding model, at most 2000 dimensions ([ADR-0012](adr/0012-configurable-embedding-width.md)) |
| Transcription | `RELAY_TRANSCRIBER`, `RELAY_WHISPERX_MODEL` | WhisperX, in `relay-ingest` | Already local; no API, no key |
| Knowledge graph, opt-in | `RELAY_MEMORY_BACKEND=graphiti-kuzu` | off ([ADR-0005](adr/0005-team-memory-without-a-graph.md)) | Uses Graphiti's built-in OpenAI clients; this page does not cover them |

Standups and the team digest use no model; Relay builds them from events.

Model names are [Pydantic AI](https://pydantic.dev/docs/ai/models/overview/) strings,
`provider:model`: `openai:gpt-5-mini`, `anthropic:claude-sonnet-5`, `ollama:qwen3`. Set
`RELAY_EMBEDDING_MODEL` to empty to turn the vector leg off; `recall` then uses lexical search
alone.

**Extraction is where model quality shows.** A wrong action item lands in someone's agent as
work to do. Pydantic AI asks for the structured result through a tool call, so the model must
support tool calling. Small models can support tools and still fail to fill in the result. Try a
model on the synthetic transcript (see [Check that it works](#check-that-it-works)) before you
give it the team's meetings.

## Run models on your own server

Use the `ollama:` or `vllm:` prefix. Each reads its own server URL, so each job can go to a
different place: for example, local embeddings and hosted extraction. `docker-compose.yml`
passes these variables to `relay-workers` and nothing else.

| Prefix | Server URL | Key |
|---|---|---|
| `ollama:<model>` | `OLLAMA_BASE_URL` | none for a local Ollama |
| `vllm:<model>` | `VLLM_BASE_URL` | `VLLM_API_KEY`, if you started vLLM with `--api-key` |

Both URLs end in `/v1`, the server's OpenAI-compatible API. The URL must work from **inside the
`relay-workers` container**, so `localhost` there means the container, not your machine:

- **Model server on the Relay node itself:** use `host.docker.internal`, which compose maps to the
  host. On Docker Desktop (macOS, Windows), this reaches a server listening on the host's
  `localhost`. On Linux, the server must listen on an address the container can reach, such as
  the Docker bridge address. Ollama listens on `127.0.0.1` by default; change that with
  `OLLAMA_HOST` ([Ollama FAQ](https://docs.ollama.com/faq)). Ollama has no authentication, so do
  not open it on `0.0.0.0` on a machine reachable from the internet.
- **Model server on another machine,** such as the GPU worker: use its Tailscale IP, the same
  way that worker reaches the node.

### Ollama

On the model server, pull an embedding model and a chat model that supports tools (the model's
page on [ollama.com](https://ollama.com/search) lists its capabilities):

```bash
ollama pull nomic-embed-text     # 768 dimensions
ollama pull qwen3                # tool calling
```

Then, in the node's `.env`:

```bash
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
RELAY_EXTRACTION_MODEL=ollama:qwen3
RELAY_EMBEDDING_MODEL=ollama:nomic-embed-text
RELAY_EMBEDDING_DIM=768
```

`mxbai-embed-large` is another option, at 1024 dimensions. A chat model without tool support fails
extraction with an error that says so; for example, Ollama rejects `llama3:8b` with "does not
support tools". A very small model can support tools and still fail: `qwen3:0.6b` ran out of
retries without producing a valid result.

### vLLM

One vLLM server usually serves one model, and `VLLM_BASE_URL` is a single URL. So use vLLM for
one job and another prefix for the other, or put a router in front that serves both.

- **Embeddings:** start vLLM with the model as a pooling (embedding) model, which serves
  `/v1/embeddings`. The flag has changed between versions (`--runner pooling`, earlier
  `--task embed`); see [vLLM's embedding docs](https://docs.vllm.ai/en/latest/models/pooling_models/embed/).
- **Extraction:** start vLLM with tool calling on (`--enable-auto-tool-choice` and the
  `--tool-call-parser` for the model's family); see
  [vLLM's tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling/).

```bash
VLLM_BASE_URL=http://100.64.0.7:8000/v1          # the GPU machine's Tailscale IP
RELAY_EMBEDDING_MODEL=vllm:BAAI/bge-m3
RELAY_EMBEDDING_DIM=1024
```

### Why not `OPENAI_BASE_URL`

The OpenAI client reads `OPENAI_BASE_URL`, but compose does not pass it to the workers, for two
reasons. It reroutes every `openai:` model at once: extraction, embeddings, and the opt-in graph.
And when it is set but empty, the client uses an empty URL instead of OpenAI's. The `ollama:` and
`vllm:` prefixes have neither problem. To use another OpenAI-compatible server anyway, add the
variable to `relay-workers` in a `docker-compose.override.yml`.

## Changing the embedding model

Set `RELAY_EMBEDDING_MODEL` and `RELAY_EMBEDDING_DIM` together, then restart:
`docker compose up -d`. If the width changed, relay-api's `relay migrate` resizes
`events.embedding` and drops the stored vectors. The workers then re-embed the log, 200 events
every 15 minutes. To do it all at once, run:

```bash
docker compose exec relay-workers relay replay --rebuild-graph
```

If the new model has the **same width** as the old one, nothing is resized, and old and new
vectors would mix in one search. Run the replay above after every model change, not only after a
width change.

Models wider than 2000 dimensions, such as `text-embedding-3-large` at 3072, do not fit pgvector's
index. Pick a narrower model.

## Check that it works

The workers log one line about the embedder when they start:

```bash
docker compose logs relay-workers | grep embeddings.
# embeddings.enabled  model=ollama:nomic-embed-text dim=768
# embeddings.disabled reason=...   (the reason names what to fix)
```

To try extraction, upload the synthetic transcript and look at what came out:

```bash
relay meeting upload --transcript fixtures/transcript_sample.json --skip-asr --participants ada,grace,linus
relay meeting status <meeting id>      # done, or failed with the model's error
relay events --since 1h
```

A usable model finds what the keyword extractor finds: two decisions, two action items with
`grace` and `linus` as assignees, and one question. A stronger one also catches the softer
"I'll check the grant line" follow-up ([fixtures/README.md](../fixtures/README.md)). For scale,
`qwen3:4b` under Ollama on a laptop found the first five, not the follow-up, and took under
three minutes.
