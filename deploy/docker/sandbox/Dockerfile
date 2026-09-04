# Sandbox for headless coding-agent runs: claude -p, codex exec, opencode run. No secrets baked in;
# the user's tokens (RELAY_TOKEN, GH_TOKEN, the model key for the chosen agent) arrive per run.
FROM node:22-bookworm-slim
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy PATH="/opt/relay/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git gnupg python3 ripgrep \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai && npm cache clean --force
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /opt/relay
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --create-home agent && mkdir -p /work && chown agent:agent /work
USER agent
WORKDIR /work
# Coding agents find Relay via a project-level MCP config written by `relay setup-agent`:
COPY --chown=agent:agent deploy/docker/sandbox/mcp.json /work/.mcp.json
CMD ["bash"]
