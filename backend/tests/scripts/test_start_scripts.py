"""Tests for the container entrypoint scripts.

init.sh owns migrations and seeds so an orchestrator can run them exactly once, as a
standalone task, instead of having every API replica race on Alembic. app.sh keeps
delegating to it so `docker compose up` behaves as before.
"""

import os
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "start"


def test_init_script_owns_migrations_and_seeds() -> None:
    init_sh = (SCRIPTS_DIR / "init.sh").read_text()

    assert "alembic upgrade head" in init_sh
    assert "scripts/init/seed_admin.py" in init_sh
    assert "scripts/init/seed_series_types.py" in init_sh


def test_init_script_is_executable() -> None:
    assert os.access(SCRIPTS_DIR / "init.sh", os.X_OK)


def test_app_script_delegates_initialization_and_starts_the_server() -> None:
    app_sh = (SCRIPTS_DIR / "app.sh").read_text()

    assert "scripts/start/init.sh" in app_sh
    assert "fastapi run app/main.py" in app_sh
    # The init steps must live in exactly one place.
    assert "alembic upgrade head" not in app_sh
    assert "seed_admin.py" not in app_sh


def test_worker_script_queue_list_is_configurable() -> None:
    """A single worker.sh backs multiple ECS services (default vs. bulk backfill),
    each listening to a different queue subset — so the queue list must be an
    override, not a hardcoded value."""
    worker_sh = (SCRIPTS_DIR / "worker.sh").read_text()

    assert "${CELERY_QUEUES:-default,sdk_sync,garmin_sync,webhook_sync}" in worker_sh
    # The old hardcoded invocation must be gone, or the override is a no-op.
    assert "-Q default,sdk_sync,garmin_sync,webhook_sync" not in worker_sh
