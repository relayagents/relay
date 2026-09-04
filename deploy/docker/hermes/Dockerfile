# One container per teammate. Home is a per-user volume; Relay credentials arrive via --env-file.
# Hermes Agent is the reference harness (ADR-0004). Pin HERMES_REF to a tag you have tested.
FROM python:3.12-slim
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git
ARG HERMES_REF=main
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy PATH="/opt/hermes/.venv/bin:/opt/relay/.venv/bin:$PATH" HOME=/home/hermes
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
# Relay CLI (so `relay ...` works inside the agent)
WORKDIR /opt/relay
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev
# Hermes Agent
WORKDIR /opt/hermes
RUN git clone --depth 1 --branch "$HERMES_REF" "$HERMES_REPO" src && uv venv .venv && uv pip install --python .venv/bin/python ./src
# Relay bridge, skill, and default config
COPY deploy/docker/hermes/relay_bridge.py /opt/relay/relay_bridge.py
COPY deploy/docker/hermes/skills /opt/relay/skills
COPY deploy/docker/hermes/config.yaml /opt/relay/default-config.yaml
COPY deploy/docker/hermes/entrypoint.sh /entrypoint.sh
RUN useradd --create-home --home-dir /home/hermes hermes && chown -R hermes:hermes /home/hermes /opt/relay
USER hermes
WORKDIR /home/hermes
VOLUME ["/home/hermes"]
ENTRYPOINT ["/entrypoint.sh"]
