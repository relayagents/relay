#!/usr/bin/env bash
# One-shot setup for a Relay node: .env, images, first admin user.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker is required"; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2 is required"; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  pw=$(openssl rand -hex 24); pepper=$(openssl rand -hex 32)
  sed -i.bak "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${pw}/; s/^RELAY_TOKEN_PEPPER=.*/RELAY_TOKEN_PEPPER=${pepper}/" .env && rm -f .env.bak
  echo "wrote .env with generated secrets; edit RELAY_HOSTNAME / Slack / model keys, then re-run."
  exit 0
fi

echo "building and starting the Relay node..."
docker compose up -d --build
echo -n "waiting for relay-api"
for _ in $(seq 1 60); do
  if docker compose exec -T relay-api curl -fsS http://localhost:8000/health >/dev/null 2>&1; then echo " ok"; break; fi
  echo -n "."; sleep 2
done

admin="${1:-${USER:-admin}}"
echo "creating admin user '${admin}' (no Hermes container for the admin by default)..."
docker compose exec -T relay-api relay add-user "${admin}" --name "${admin}" --admin --no-container || true
echo
echo "next:"
echo "  relay login --url \$(grep ^RELAY_PUBLIC_URL .env | cut -d= -f2) --token <human token printed above>"
echo "  relay add-user <teammate>            # on the node, per teammate"
echo "  relay setup-agent claude-code        # on each laptop"
echo "  relay meeting upload --transcript fixtures/transcript_sample.json --skip-asr --participants ada,grace,linus"
