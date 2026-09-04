#!/bin/sh
# Seeds the per-user home on first start, then runs the Relay bridge (inbox loop + standup cron).
set -eu
: "${RELAY_URL:?RELAY_URL is required}"
: "${RELAY_TOKEN:?RELAY_TOKEN is required}"
mkdir -p "$HOME/.hermes/skills" "$HOME/.config/relay"
[ -f "$HOME/.hermes/config.yaml" ] || sed "s#__RELAY_URL__#${RELAY_URL}#g; s#__RELAY_TOKEN__#${RELAY_TOKEN}#g" /opt/relay/default-config.yaml > "$HOME/.hermes/config.yaml"
[ -d "$HOME/.hermes/skills/relay" ] || cp -r /opt/relay/skills/relay "$HOME/.hermes/skills/relay"
[ -f "$HOME/.config/relay/credentials.json" ] || printf '{"url": "%s", "token": "%s", "user_id": "%s"}\n' "$RELAY_URL" "$RELAY_TOKEN" "${RELAY_USER:-}" > "$HOME/.config/relay/credentials.json"
chmod 600 "$HOME/.config/relay/credentials.json"
exec python /opt/relay/relay_bridge.py "$@"
