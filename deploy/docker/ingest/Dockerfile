# relay-ingest: WhisperX + pyannote, installed exactly as locked in uv.lock.
# CPU by default; build with TORCH_VARIANT=cu128 for CUDA 12.8 (needs NVIDIA driver >= 570).
# linux/amd64 only for now: torchcodec 0.7 (paired with torch 2.8) ships no Linux arm64 wheel.
FROM python:3.12-slim
# One of the conflicting torch extras in pyproject.toml: cpu or cu128.
ARG TORCH_VARIANT=cpu
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" HF_HOME=/var/lib/relay/hf
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
# --locked fails the build if uv.lock is stale against pyproject.toml, and installs only what it pins.
# The cache mount keeps uv's download cache out of the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra ingest --extra "$TORCH_VARIANT"
# relay only writes under /var/lib/relay (audio, transcripts, HF_HOME). Leaving /app owned by root
# also avoids a chown -R that would copy the multi-GB venv into a second layer.
RUN useradd --create-home relay && mkdir -p /var/lib/relay && chown -R relay:relay /var/lib/relay
COPY deploy/docker/relay/entrypoint.sh /entrypoint.sh
USER relay
VOLUME ["/var/lib/relay"]
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ingest"]
