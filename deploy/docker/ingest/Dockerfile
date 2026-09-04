# relay-ingest: WhisperX + pyannote. CPU by default; build with TORCH_INDEX=https://download.pytorch.org/whl/cu124 for CUDA.
FROM python:3.12-slim
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" HF_HOME=/var/lib/relay/hf
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python --index-url "$TORCH_INDEX" torch torchaudio \
    && uv pip install --python /app/.venv/bin/python ".[ingest]"
RUN useradd --create-home relay && mkdir -p /var/lib/relay && chown -R relay:relay /var/lib/relay /app
COPY deploy/docker/relay/entrypoint.sh /entrypoint.sh
USER relay
VOLUME ["/var/lib/relay"]
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ingest"]
