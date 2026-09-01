#!/bin/bash
set -e -x

# Override to run a worker against a queue subset (e.g. a dedicated backfill
# worker listening only to sdk_sync) without duplicating this invocation
# elsewhere. Defaults to every queue, matching a single-worker deployment.
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q "${CELERY_QUEUES:-default,sdk_sync,garmin_sync,webhook_sync}"
