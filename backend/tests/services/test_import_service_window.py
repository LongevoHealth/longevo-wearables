"""Tests for the batch time window computed by ``ImportService.load_data``."""

from datetime import datetime, timezone
from typing import Any, Callable

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
    """La ventana es el minimo y el maximo de todo lo que toco el batch."""
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
    """Sin datos no hay ventana. El consumidor descarta ese evento."""
    user = UserFactory()

    result = import_service.load_data(db, sdk_payload_factory([]), user_id=str(user.id))

    assert result["window_start"] is None
    assert result["window_end"] is None
