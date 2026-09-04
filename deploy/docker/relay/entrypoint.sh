#!/bin/sh
set -eu
case "${1:-api}" in
  api)
    relay migrate
    exec uvicorn relayagents.api.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
    ;;
  workers)
    exec arq relayagents.workers.main.WorkerSettings
    ;;
  ingest)
    exec arq relayagents.ingest.worker.WorkerSettings
    ;;
  *)
    exec "$@"
    ;;
esac
