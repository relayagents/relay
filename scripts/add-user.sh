#!/usr/bin/env bash
# Provision a teammate from the node: user + tokens + AgentCard through relay-api, then a Hermes
# container with its own volume. The agent token is written to var/agents/<user>.env on the host
# (mode 0600, gitignored) and handed to the container as an env file; it never touches the shared
# data volume or the API container.
#
#   scripts/add-user.sh grace [--name "Grace Hopper"] [--slack-user-id U123] [--github-login grace] [--reissue]
set -euo pipefail
cd "$(dirname "$0")/.."
user="${1:?usage: scripts/add-user.sh <user-id> [relay add-user options...]}"; shift
[ -f .env ] || { echo "run scripts/bootstrap.sh first"; exit 1; }

image="$(grep -E '^RELAY_HERMES_IMAGE=' .env | cut -d= -f2-)"; image="${image:-ghcr.io/relayagents/relay-hermes:latest}"
mkdir -p var/agents && chmod 700 var/agents
env_file="var/agents/${user}.env"

# 1. user, tokens, AgentCard (JSON out; the human token is printed once at the end)
out="$(docker compose exec -T relay-api relay add-user "$user" --no-container --json "$@")"
agent_token="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_token"])')"
human_token="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["human_token"])')"
relay_url="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["relay_url"])')"
agent_id="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_id"])')"

# 2. credentials for the container, on the host only
umask 077
printf 'RELAY_URL=%s\nRELAY_TOKEN=%s\nRELAY_USER=%s\nRELAY_AGENT_ID=%s\n' "$relay_url" "$agent_token" "$user" "$agent_id" > "$env_file"

# 3. one Hermes container per teammate, own named volume, on the compose network
container="relay-hermes-${user}"
docker rm -f "$container" >/dev/null 2>&1 || true
docker run -d --name "$container" --restart unless-stopped \
  --network relay_default \
  --env-file "$env_file" \
  --mount "type=volume,source=relay_agent_${user},target=/home/hermes" \
  "$image" >/dev/null
echo "started ${container} (${agent_id}); credentials in ${env_file}"
echo
echo "== give this to ${user} (human token; used once by \`relay login --token\`) =="
echo "$human_token"
echo
echo "== then on their laptop =="
echo "relay login --url ${relay_url} --token <the token above>"
echo "relay setup-agent claude-code   # or codex / opencode"
