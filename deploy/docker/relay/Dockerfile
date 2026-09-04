# relay-api and relay-workers share this image; the entrypoint picks the role.
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra graph
RUN useradd --create-home relay && mkdir -p /var/lib/relay && chown -R relay:relay /var/lib/relay /app
COPY deploy/docker/relay/entrypoint.sh /entrypoint.sh
USER relay
VOLUME ["/var/lib/relay"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 CMD curl -fsS http://localhost:8000/health || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
