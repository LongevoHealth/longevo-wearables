import contextlib
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import cast

from celery import shared_task

from app.config import settings
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.models import EventRecord
from app.schemas.sync_status import SyncSource, SyncStatus
from app.services.apple.healthkit.sleep_service import (
    active_users_key,
    finish_sleep,
    load_sleep_state,
)
from app.services.sync_status_service import completed, new_run_id
from app.utils.sentry_helpers import log_and_capture_error

logger = getLogger(__name__)


def _announce_finalized_sleep(user_id: str, provider: str | None, record: EventRecord) -> None:
    """Emit a terminal sync-status event for a session finalized out of band.

    `handle_sleep_data` only writes a session synchronously when it is
    already stale; a live-night session instead accumulates in Redis and is
    written here, later, with no event of its own — `finish_sleep` emits
    nothing. A consumer that already pulled the original event's window
    finds nothing (the session was still invisible), and without this call
    nothing ever tells it the data later became available.

    `provider` comes from the Redis-tracked `SleepState`, not the persisted
    `EventRecord`: `provider` is only used at create-time to resolve
    `data_source_id` and is not itself a column on the row `finish_sleep`
    returns.

    Metadata mirrors `process_sdk_upload_task`'s COMPLETED shape exactly —
    `records_saved` / `workouts_saved` / `sleep_saved` / `types` /
    `window_start` / `window_end` — since the consumer's parser was built
    against that shape and requires a usable window.

    A failure here is logged and swallowed: the sleep row itself is already
    safely written by `finish_sleep` by the time this runs, and a broken
    announcement must never undo that write or stop the next user in the
    loop from being processed.
    """
    try:
        completed(
            user_id,
            provider or "unknown",
            SyncSource.SDK,
            run_id=new_run_id(),
            status=SyncStatus.SUCCESS,
            message="Sleep session finalized out of band",
            items_processed=1,
            metadata={
                "records_saved": 0,
                "workouts_saved": 0,
                "sleep_saved": 1,
                "types": [],
                "dropped_count": 0,
                "window_start": record.start_datetime,
                "window_end": record.end_datetime,
            },
        )
    except Exception as e:
        log_and_capture_error(
            e,
            logger,
            f"Error announcing finalized sleep for user {user_id}: {e}",
            extra={"user_id": user_id},
        )


@shared_task
def finalize_stale_sleeps() -> None:
    now = datetime.now(timezone.utc)
    redis_client = get_redis_client()

    with SessionLocal() as db:
        for user_id in cast(set[str], redis_client.smembers(active_users_key())):
            try:
                # Skip users whose upload is currently in progress.
                lock = redis_client.lock(f"sleep:lock:{user_id}", timeout=30, blocking_timeout=0)
                if not lock.acquire(blocking=False):
                    continue

                try:
                    state = load_sleep_state(user_id)
                    if not state:
                        continue

                    end_time = state.end_time
                    if end_time.tzinfo is None:
                        end_time = end_time.replace(tzinfo=timezone.utc)

                    if now - end_time >= timedelta(minutes=settings.sleep_end_gap_minutes):
                        finalized_record = finish_sleep(db, user_id, state)
                        if finalized_record is not None:
                            _announce_finalized_sleep(user_id, state.provider, finalized_record)
                finally:
                    with contextlib.suppress(Exception):
                        lock.release()

            except Exception as e:
                log_and_capture_error(
                    e,
                    logger,
                    f"Error finalizing stale sleep for user {user_id}: {e}",
                    extra={"user_id": user_id},
                )
