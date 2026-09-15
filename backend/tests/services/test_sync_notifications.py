import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.schemas.sync_status import SyncSource, SyncStage, SyncStatus, SyncStatusEvent
from app.services.outgoing_webhooks import sync_notifications

USER = UUID("11111111-1111-1111-1111-111111111111")


def _event(**overrides: Any) -> SyncStatusEvent:
    base = dict(
        run_id="b1",
        user_id=USER,
        provider="apple",
        source=SyncSource.SDK,
        stage=SyncStage.COMPLETED,
        status=SyncStatus.SUCCESS,
        items_processed=1243,
        metadata={
            "types": ["steps", "heart_rate"],
            "records_saved": 1240,
            "workouts_saved": 2,
            "sleep_saved": 1,
            "window_start": datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc),
            "window_end": datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc),
        },
        ended_at=datetime(2026, 9, 11, 14, 3, 12, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SyncStatusEvent(**base)


def test_payload_matches_the_v1_contract() -> None:
    payload = sync_notifications.build_payload(_event(), external_user_id="uw-123")

    with open("tests/fixtures/sync_completed_v1.json") as handle:
        expected = json.load(handle)

    assert payload == expected


def test_payload_never_carries_health_data() -> None:
    """El evento dice qué cambió y dónde buscarlo. Nunca el valor."""
    payload = sync_notifications.build_payload(_event(), external_user_id="uw-123")

    # Ninguna clave del contrato es una lista de muestras o un valor de medición:
    # solo identificadores, timestamps de ventana y conteos agregados.
    allowed_keys = {
        "event_type",
        "schema_version",
        "user_id",
        "external_user_id",
        "provider",
        "batch_id",
        "occurred_at",
        "metrics",
        "activity_types",
        "earliest_record_start_at",
        "latest_record_end_at",
        "counts",
    }
    assert set(payload.keys()) <= allowed_keys
    assert set(payload["counts"].keys()) <= {"records", "workouts", "sleep"}
    assert all(isinstance(v, int) for v in payload["counts"].values())

    forbidden_keys = {"samples", "value", "values", "data", "reading", "readings", "measurements"}
    assert forbidden_keys.isdisjoint(payload.keys())
    assert forbidden_keys.isdisjoint(payload["counts"].keys())


def test_payload_tolerates_a_missing_window() -> None:
    payload = sync_notifications.build_payload(_event(metadata={"types": []}), external_user_id="uw-123")

    assert payload["earliest_record_start_at"] is None
    assert payload["latest_record_end_at"] is None


def test_payload_tolerates_a_missing_external_user_id() -> None:
    payload = sync_notifications.build_payload(_event(), external_user_id=None)

    assert payload["external_user_id"] is None
    assert payload["user_id"] == str(USER)


def test_payload_is_identical_whether_the_window_survived_a_json_round_trip() -> None:
    """metadata es dict[str, Any]: tras `model_dump_json` + `model_validate_json`
    (lo que hace la Celery task), `window_start`/`window_end` dejan de ser
    `datetime` y pasan a ser el string que produjo pydantic. build_payload debe
    devolver exactamente lo mismo en ambos casos."""
    event = _event()
    round_tripped = SyncStatusEvent.model_validate_json(event.model_dump_json())

    assert isinstance(event.metadata["window_start"], datetime)
    assert isinstance(round_tripped.metadata["window_start"], str)

    direct_payload = sync_notifications.build_payload(event, external_user_id="uw-123")
    round_tripped_payload = sync_notifications.build_payload(round_tripped, external_user_id="uw-123")

    assert direct_payload == round_tripped_payload


def test_activity_types_can_be_declared_explicitly() -> None:
    """El pull declara sus actividades; no se infieren de conteos que no tiene.

    El camino de pull de proveedores no sabe cuántos sueños o workouts
    escribió —cada proveedor devuelve una forma distinta— pero sí sabe qué
    sub-sincronizaciones corrió. Declararlas explícitamente evita la
    alternativa deshonesta: inventar un `sleep_saved: 1` para que el
    consumidor puleé sueño.
    """
    event = _event(
        metadata={
            "types": ["recovery"],
            "activity_types": ["sleep", "workout"],
            "window_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
            "window_end": datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc),
        }
    )

    payload = sync_notifications.build_payload(event, external_user_id="uw-123")

    assert payload["activity_types"] == ["sleep", "workout"]
    # Los conteos siguen siendo los reales: cero, porque no se conocen.
    assert payload["counts"] == {"records": 0, "workouts": 0, "sleep": 0}


def test_activity_types_still_fall_back_to_the_counts() -> None:
    """Sin declaración explícita, el camino del SDK sigue derivando de conteos."""
    payload = sync_notifications.build_payload(_event(), external_user_id="uw-123")

    assert payload["activity_types"] == ["sleep", "workout"]
