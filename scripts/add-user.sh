#!/usr/bin/env bash
# Create a teammate from the node: user record and human token through relay-api. Agents are not
# provisioned here; each person connects their own coding agent per project with
# `relay setup-agent`, which mints that agent's token (ADR-0010, ADR-0011).
#
#   scripts/add-user.sh grace [--name "Grace Hopper"] [--slack-user-id U123] [--github-login grace] [--reissue]
set -euo pipefail
cd "$(dirname "$0")/.."
user="${1:?usage: scripts/add-user.sh <user-id> [relay add-user options...]}"; shift
[ -f .env ] || { echo "run scripts/bootstrap.sh first"; exit 1; }

out="$(docker compose exec -T relay-api relay add-user "$user" --json "$@")"
human_token="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["human_token"])')"
relay_url="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["relay_url"])')"

echo "created ${user}"
echo
echo "== give this to ${user} (human token; used once by \`relay login --token\`) =="
echo "$human_token"
echo
echo "== then on their laptop =="
echo "relay login --url ${relay_url} --token <the token above>"
echo "cd <a project that should use Relay> && relay setup-agent claude-code --write"
echo "  # or codex / opencode / cursor, with --env-file <a gitignored .envrc, or their shell profile>"
