"""Tests for the batch time window computed by ``ImportService.load_data``."""

from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.apple.healthkit.import_service import import_service
from tests.factories import UserFactory

SDK_ENVELOPE: dict[str, str] = {
    "provider": "apple",
    "sdkVersion": "1.0.0",
    "syncTimestamp": "2026-09-11T00:00:00Z",
}

_METRIC_TYPE_BY_SERIES = {
    "steps": "HKQuantityTypeIdentifierStepCount",
    "heart_rate": "HKQuantityTypeIdentifierHeartRate",
}


@pytest.fixture
def sdk_payload_factory() -> Callable[[list[tuple[str, datetime, float]]], dict[str, Any]]:
    """Build a minimal Apple SDK payload from a list of (series, recorded_at, value) samples."""

    def _build(samples: list[tuple[str, datetime, float]]) -> dict[str, Any]:
        records = [
            {
                "id": f"sample-{index}",
                "type": _METRIC_TYPE_BY_SERIES[series],
                "unit": "count",
                "value": value,
                "startDate": recorded_at.isoformat(),
                "endDate": recorded_at.isoformat(),
                "source": {"name": "Test Device", "bundleIdentifier": "test"},
            }
            for index, (series, recorded_at, value) in enumerate(samples)
        ]
        return {**SDK_ENVELOPE, "data": {"records": records}}

    return _build


def test_load_data_returns_batch_window(
    db: Session, sdk_payload_factory: Callable[[list[tuple[str, datetime, float]]], dict[str, Any]]
) -> None:
    """The window is the min and max of everything the batch touched."""
    user = UserFactory()
    raw = sdk_payload_factory(
        [
            ("steps", datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc), 100),
            ("steps", datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc), 240),
            ("heart_rate", datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc), 62),
        ]
    )

    result = import_service.load_data(db, raw, user_id=str(user.id), batch_id="b1")

    assert result["window_start"] == datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc)
    assert result["window_end"] == datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc)


def test_load_data_window_is_none_when_batch_saved_nothing(
    db: Session, sdk_payload_factory: Callable[[list[tuple[str, datetime, float]]], dict[str, Any]]
) -> None:
    """No data means no window. The consumer discards that event."""
    user = UserFactory()

    result = import_service.load_data(db, sdk_payload_factory([]), user_id=str(user.id))

    assert result["window_start"] is None
    assert result["window_end"] is None


@pytest.fixture
def workout_payload_factory() -> Callable[[datetime, datetime], dict[str, Any]]:
    """Build a minimal Apple SDK payload with a single, stat-free workout spanning [start, end]."""

    def _build(start: datetime, end: datetime) -> dict[str, Any]:
        workout = {
            "id": "workout-0",
            "type": "walking",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "source": {"name": "Test Watch", "bundleIdentifier": "test"},
        }
        return {**SDK_ENVELOPE, "data": {"workouts": [workout]}}

    return _build


def test_load_data_window_covers_workout_records(
    db: Session, workout_payload_factory: Callable[[datetime, datetime], dict[str, Any]]
) -> None:
    """The workout-widen loop (``import_service.py`` ~404-405) must widen the window to the
    workout record's own start/end. The workout here carries no `values` statistics, so no
    time-series sample can widen the window instead -- this isolates the workout-record path.
    """
    user = UserFactory()
    start = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 5, 6, 45, tzinfo=timezone.utc)

    result = import_service.load_data(db, workout_payload_factory(start, end), user_id=str(user.id))

    assert result["workouts_saved"] == 1
    assert result["window_start"] == start
    assert result["window_end"] == end


@pytest.fixture
def sleep_payload_factory() -> Callable[[list[tuple[str, datetime, datetime]]], dict[str, Any]]:
    """Build a minimal Apple SDK payload from a list of (stage, start, end) sleep segments."""

    def _build(segments: list[tuple[str, datetime, datetime]]) -> dict[str, Any]:
        sleep = [
            {
                "id": f"sleep-{index}",
                "stage": stage,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "source": {"name": "Test Device", "bundleIdentifier": "test"},
            }
            for index, (stage, start, end) in enumerate(segments)
        ]
        return {**SDK_ENVELOPE, "data": {"sleep": sleep}}

    return _build


@patch("app.services.apple.healthkit.sleep_service.event_record_service")
@patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
@patch("app.services.apple.healthkit.sleep_service.get_redis_client")
def test_load_data_sleep_window_comes_from_request_not_merged_persisted_record(
    mock_get_redis: MagicMock,
    mock_finalize: MagicMock,
    mock_event_service: MagicMock,
    db: Session,
    sleep_payload_factory: Callable[[list[tuple[str, datetime, datetime]]], dict[str, Any]],
) -> None:
    """The sleep-widen loop (``import_service.py`` ~419-420) must widen the window from the
    *request*'s own sleep segments, not from whatever ``handle_sleep_data`` ends up persisting.

    ``finish_sleep`` can merge the incoming session with an adjacent one already in the
    database, producing a persisted record that spans a much wider interval than this
    request touched. We simulate exactly that: ``find_adjacent_sleep_record`` returns an
    existing record from 12:00-20:00, while the incoming request only carries a 22:00-22:30
    segment. A correct implementation reports the batch window as [22:00, 22:30]; an
    implementation that reads the window off the persisted/merged row would report
    [12:00, 22:30] instead -- this test fails against that regression.
    """
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.expire.return_value = True
    mock_redis.sadd.return_value = 1
    mock_redis.srem.return_value = 1
    mock_get_redis.return_value = mock_redis

    mock_record = MagicMock()
    mock_record.id = uuid4()
    mock_event_service.create.return_value = mock_record

    # An existing, much wider session already sitting in the DB, adjacent to the incoming batch.
    mock_adjacent = MagicMock()
    mock_adjacent.start_datetime = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    mock_adjacent.end_datetime = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    mock_adjacent.sleep_detail = None
    mock_event_service.find_adjacent_sleep_record.return_value = mock_adjacent

    user = UserFactory()
    request_start = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 9, 1, 22, 30, tzinfo=timezone.utc)

    result = import_service.load_data(
        db, sleep_payload_factory([("in_bed", request_start, request_end)]), user_id=str(user.id)
    )

    # Sanity check: the merge this test relies on actually happened, i.e. this test would be
    # pointless if `finish_sleep` created a record scoped to only the request's own segment.
    created_record_kwargs = mock_event_service.create.call_args.args[1]
    assert created_record_kwargs.start_datetime == mock_adjacent.start_datetime
    assert created_record_kwargs.end_datetime == request_end

    assert result["sleep_saved"] == 1
    assert result["window_start"] == request_start
    assert result["window_end"] == request_end
