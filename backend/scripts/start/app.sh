#!/bin/bash
set -e -x

# Migrations and seeds. On an orchestrator this same script runs as a one-off task
# before the rollout, and the API service starts the server directly — so this call
# is what keeps `docker compose up` working end to end.
bash scripts/start/init.sh

# Init app
echo "Starting the FastAPI application..."
if [ "$ENVIRONMENT" = "local" ]; then
    uv run fastapi dev app/main.py --host 0.0.0.0 --port "${API_PORT:-8000}"
else
    uv run fastapi run app/main.py --host 0.0.0.0 --port "${API_PORT:-8000}"
fi
