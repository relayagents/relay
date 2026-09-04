#!/usr/bin/env bash
# One-shot setup for a Relay node: .env, images, first admin user.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker is required"; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2 is required"; exit 1; }

gen_secret() {  # gen_secret NAME BYTES — set NAME in .env if it is missing or still a placeholder
  if ! grep -qE "^$1=[^[:space:]#]" .env || grep -qE "^$1=change-me([[:space:]]|$)" .env; then
    v=$(openssl rand -hex "$2")
    if grep -qE "^$1=" .env; then sed -i.bak "s/^$1=.*/$1=${v}/" .env && rm -f .env.bak; else printf '%s=%s\n' "$1" "$v" >> .env; fi
    echo "generated $1"
  fi
}

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  gen_secret POSTGRES_PASSWORD 24; gen_secret REDIS_PASSWORD 24; gen_secret RELAY_TOKEN_PEPPER 32
  echo "wrote .env with generated secrets; edit RELAY_HOSTNAME / Slack / model keys, then re-run."
  exit 0
fi
# Upgrades: fill in secrets that newer versions require (e.g. REDIS_PASSWORD) without touching the rest.
gen_secret REDIS_PASSWORD 24; gen_secret RELAY_TOKEN_PEPPER 32

echo "building and starting the Relay node..."
docker compose up -d --build
echo -n "waiting for relay-api"
for _ in $(seq 1 60); do
  if docker compose exec -T relay-api curl -fsS http://localhost:8000/health >/dev/null 2>&1; then echo " ok"; break; fi
  echo -n "."; sleep 2
done

admin="${1:-owner}"
echo "creating admin user '${admin}' (no Hermes container for the admin)..."
if ! docker compose exec -T relay-api relay add-user "${admin}" --name "${admin}" --admin --no-container; then
  echo "admin user not created (already exists? re-run with: scripts/bootstrap.sh <id> after 'relay add-user <id> --reissue')"
fi
echo
echo "next:"
echo "  relay login --url \$(grep ^RELAY_PUBLIC_URL .env | cut -d= -f2) --token <human token printed above>"
echo "  scripts/add-user.sh <teammate>       # on the node, per teammate (user, tokens, AgentCard, Hermes container)"
echo "  relay setup-agent claude-code        # on each laptop"
echo "  relay meeting upload --transcript fixtures/transcript_sample.json --skip-asr --participants ada,grace,linus"
