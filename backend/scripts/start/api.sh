#!/bin/bash
set -e -x

# The API server on its own. Unlike app.sh — which docker compose uses and which
# still runs init.sh first — this deliberately skips migrations and seeds: on an
# orchestrator those run exactly once as a standalone task before the rollout, so
# API replicas never race on Alembic.
uv run fastapi run app/main.py --host 0.0.0.0 --port "${API_PORT:-8000}"
