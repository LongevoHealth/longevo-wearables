# Ingesta Open Wearables → Longevo · Fase 1 (camino SDK)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que un usuario de Apple Health, Health Connect o Samsung Health sincronice contra Open Wearables y su dato llegue al dominio de Longevo en QA, por un evento sin PHI más un pull autenticado.

**Architecture:** Open Wearables emite un evento coalescido `sync.completed` desde `sync_status_service.emit_event` —el único seam por el que pasan sus cuatro caminos de ingesta— hacia un topic SNS FIFO. Una cola SQS FIFO ordenada por usuario lo entrega a `wearables-api`, que resuelve el detalle contra la External API con una API key de lectura y persiste en su DocumentDB actual. El evento no lleva dato de salud; el dato viaja sólo por el pull.

**Tech Stack:** Python 3.13 / FastAPI / Celery / SQLAlchemy (fork) · NestJS / TypeScript / Jest (monorepo) · Terraform (longevoIac) · SNS FIFO, SQS FIFO, IAM, WAF.

**Spec:** [`docs/superpowers/specs/2026-09-11-ingesta-open-wearables-longevo-design.md`](../specs/2026-09-11-ingesta-open-wearables-longevo-design.md)

## Global Constraints

- **Tres repos.** `longevo-wearables` (fork, Python), `monorepo-backend` (NestJS, rama `feat/openwearables-integration`), `longevoIac` (Terraform). Cada tarea dice en cuál trabaja.
- **Sólo ambiente `qa`.** `src/open-wearables/environments/` no tiene `prod` todavía.
- **Fase 1 no toca el store de Longevo.** El DocumentDB `wearables-v2`, el matching de actividades, los resúmenes B2B y la publicación de user actions quedan como están. La única excepción es el writer de sueño (Task 8), y por la razón de la sección 7 del spec.
- **Contrato del evento, versión 1.** Idéntico en productor, consumidor y fixtures:

```json
{
  "event_type": "sync.completed",
  "schema_version": 1,
  "user_id": "<uuid interno de Open Wearables>",
  "external_user_id": "<userWearableId de Longevo, o null>",
  "provider": "apple",
  "batch_id": "<run_id>",
  "occurred_at": "2026-09-11T14:03:12Z",
  "metrics": ["steps", "heart_rate"],
  "activity_types": ["sleep", "running"],
  "earliest_record_start_at": "2026-09-10T22:14:00Z",
  "latest_record_end_at": "2026-09-11T13:58:00Z",
  "counts": { "records": 1240, "workouts": 2, "sleep": 1 }
}
```

- **El evento nunca lleva dato de salud.** Ni muestras, ni valores, ni nombres de registro. Sólo qué cambió y en qué ventana.
- **Un evento sin ventana se descarta, no se puleea a ciegas.** Descartar incrementa una métrica que tiene alarma. Un pull sin ventana contra un usuario con años de historia es un incidente.
- **Cola FIFO:** `MessageGroupId = external_user_id`, `MessageDeduplicationId = batch_id`.
- **Fork:** todas las funciones con type hints. `cd backend && uv run pre-commit run --all-files` antes de cada commit.
- **Monorepo:** `pnpm run lint:fix && pnpm run format` antes de cada commit.
- **Mínima divergencia contra upstream.** Cada cambio en el fork tiene que tener forma de algo que upstream aceptaría.

---

## File Structure

**Fork (`longevo-wearables/backend/`)**

| Archivo | Responsabilidad |
|---|---|
| `app/services/apple/healthkit/import_service.py` (modificar) | Calcular la ventana temporal del batch y devolverla en el resultado |
| `app/schemas/responses/upload/…` (modificar) | `UploadDataResponse` transporta la ventana |
| `app/integrations/celery/tasks/process_sdk_upload_task.py` (modificar) | Pasar la ventana al `metadata` de `completed()` |
| `app/services/outgoing_webhooks/sync_notifications.py` (crear) | Construir y publicar el evento a SNS. Único lugar que conoce el contrato |
| `app/integrations/celery/tasks/publish_sync_notification_task.py` (crear) | Resolver `external_user_id` y publicar, fuera del camino de ingesta |
| `app/services/sync_status_service.py` (modificar) | Un hook de tres líneas en `emit_event`, filtrado por stage y status |
| `app/config.py` (modificar) | `sync_notifications_enabled`, `aws_sync_events_topic_arn` |
| `tests/fixtures/sync_completed_v1.json` (crear) | Fixture del contrato, espejado en el monorepo |

**Infra (`longevoIac/src/`)**

| Archivo | Responsabilidad |
|---|---|
| `open-wearables/environments/qa/sync-events.tf` (crear) | Topic SNS FIFO, CMK, `sns:Publish` en el rol de task del worker |
| `longevo/wearables/environments/qa/open-wearables-sync-events.tf` (crear) | Cola SQS FIFO, queue policy, suscripción, permisos de consumo |
| `open-wearables/environments/qa/dns-alb-waf.tf` (modificar) | Regla de método y path |

**Monorepo (`monorepo-backend/`)**

| Archivo | Responsabilidad |
|---|---|
| `apps/wearables-api/src/dtos/openwearables-sync-event.ts` (crear) | Contrato y política de descarte, como función pura |
| `apps/wearables-api/src/services/openwearables-sync-queue.handler.ts` (crear) | Consumir la cola y rutear al use case |
| `libs/wearables-v2-sdk/src/infraestructure/openwearables-sleep-writer.service.ts` (crear) | Snapshot-replace por ventana |
| `apps/wearables-api/src/use-cases/handle-openwearables-sync.use-case.ts` (crear) | Los tres pulls, los writers y la compuerta por fuente default |
| `libs/wearables-sdk/src/core/entities/wearable-source-type.ts` (modificar) | `OPEN_WEARABLES` |
| `libs/wearables-sdk/src/infraestructure/default-wearable-source.service.ts` (modificar) | Precedencia y predicado de "produciendo" |
| `apps/wearables-api/src/wearables-api.module.ts` (modificar) | Registrar cola, handler y use case |
| `apps/wearables-api/test/fixtures/sync-completed-v1.json` (crear) | El mismo fixture que el fork |

---

## Fase A — Productor (repo `longevo-wearables`)

### Task 1: Ventana temporal del batch

**Files:**
- Modify: `backend/app/services/apple/healthkit/import_service.py` (`load_data`, ~340-410)
- Modify: `backend/app/schemas/responses/upload/` (`UploadDataResponse`)
- Modify: `backend/app/integrations/celery/tasks/process_sdk_upload_task.py:146-175`
- Test: `backend/tests/services/test_import_service_window.py`

**Interfaces:**
- Consumes: nada.
- Produces: `load_data()` devuelve además `window_start: datetime | None` y `window_end: datetime | None`. `UploadDataResponse` gana los mismos dos campos. El `metadata` de `completed()` gana `window_start` y `window_end` como strings ISO 8601 en UTC, o ausentes si no hubo datos.

- [ ] **Step 1: Escribir el test que falla**

```python
# backend/tests/services/test_import_service_window.py
from datetime import datetime, timezone

from app.services.apple.healthkit.import_service import import_service


def test_load_data_returns_batch_window(db_session, sdk_payload_factory):
    """La ventana es el mínimo y el máximo de todo lo que tocó el batch."""
    raw = sdk_payload_factory(
        samples=[
            ("steps", datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc), 100),
            ("steps", datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc), 240),
            ("heart_rate", datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc), 62),
        ]
    )

    result = import_service.load_data(db_session, raw, user_id=str(TEST_USER_ID), batch_id="b1")

    assert result["window_start"] == datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc)
    assert result["window_end"] == datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc)


def test_load_data_window_is_none_when_batch_saved_nothing(db_session, sdk_payload_factory):
    """Sin datos no hay ventana. El consumidor descarta ese evento."""
    result = import_service.load_data(db_session, sdk_payload_factory(samples=[]), user_id=str(TEST_USER_ID))

    assert result["window_start"] is None
    assert result["window_end"] is None
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `cd backend && uv run pytest tests/services/test_import_service_window.py -v`
Expected: FAIL con `KeyError: 'window_start'`.

- [ ] **Step 3: Trackear la ventana en `load_data`**

En `load_data`, junto a `types: set[str] = set()`:

```python
window_start: datetime | None = None
window_end: datetime | None = None

def _widen(start: datetime, end: datetime | None = None) -> None:
    """Ensanchar la ventana del batch con el intervalo de un registro."""
    nonlocal window_start, window_end
    finish = end or start
    if window_start is None or start < window_start:
        window_start = start
    if window_end is None or finish > window_end:
        window_end = finish
```

Después de cada `bulk_create_samples`, sobre la lista que se acaba de insertar:

```python
for sample in time_series_samples:
    _widen(sample.recorded_at)
```

```python
for sample in samples:
    _widen(sample.recorded_at)
```

Para workouts, sobre los bundles ya construidos:

```python
for record, _, _ in workout_bundles:
    _widen(record.start_datetime, record.end_datetime)
```

Para sueño, dentro del bloque `if request.data.sleep:`, sobre los segmentos del request (no sobre lo persistido: `handle_sleep_data` puede fusionar, y la ventana que le importa al consumidor es la del dato que entró):

```python
for segment in request.data.sleep:
    _widen(segment.start_time, segment.end_time)
```

Y en el `return`:

```python
return {
    "workouts_saved": workouts_saved,
    "records_saved": records_saved,
    "types": sorted(types),
    "sleep_saved": sleep_saved,
    "dropped": dropped,
    "validation_ms": validation_ms,
    "window_start": window_start,
    "window_end": window_end,
}
```

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `cd backend && uv run pytest tests/services/test_import_service_window.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Propagar por `UploadDataResponse` y el task**

En `UploadDataResponse`, agregar:

```python
window_start: datetime | None = None
window_end: datetime | None = None
```

En `import_data_from_request`, al construir la respuesta exitosa:

```python
window_start=saved_counts["window_start"],
window_end=saved_counts["window_end"],
```

En `process_sdk_upload_task.py`, dentro del `metadata` de la llamada a `completed(...)`:

```python
metadata={
    "batch_id": batch_id,
    "records_saved": records_saved,
    "workouts_saved": workouts_saved,
    "sleep_saved": sleep_saved,
    "types": types,
    "dropped_count": dropped_count,
    "window_start": result.get("window_start"),
    "window_end": result.get("window_end"),
},
```

- [ ] **Step 6: Test de extremo a extremo del task**

```python
# backend/tests/tasks/test_process_sdk_upload_window.py
from unittest.mock import patch


def test_completed_metadata_carries_the_window(db_session, seeded_user, sdk_payload_json):
    with patch("app.integrations.celery.tasks.process_sdk_upload_task.completed") as mock_completed:
        process_sdk_upload(
            content=sdk_payload_json,
            content_type="application/json",
            user_id=str(seeded_user.id),
            provider="apple",
            batch_id="b1",
        )

    metadata = mock_completed.call_args.kwargs["metadata"]
    assert metadata["window_start"] is not None
    assert metadata["window_end"] >= metadata["window_start"]
    assert metadata["types"]
```

Run: `cd backend && uv run pytest tests/tasks/test_process_sdk_upload_window.py -v`
Expected: PASS.

- [ ] **Step 7: Lint y commit**

```bash
cd backend && uv run pre-commit run --all-files
git add backend/app backend/tests
git commit -m "feat(sdk): compute and propagate the ingest batch time window"
```

---

### Task 2: Publisher SNS del evento

**Files:**
- Create: `backend/app/services/outgoing_webhooks/sync_notifications.py`
- Create: `backend/app/integrations/celery/tasks/publish_sync_notification_task.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/integrations/celery/tasks/__init__.py`
- Create: `backend/tests/fixtures/sync_completed_v1.json`
- Test: `backend/tests/services/test_sync_notifications.py`

**Interfaces:**
- Consumes: el `metadata` con `window_start` / `window_end` / `types` de Task 1.
- Produces: `sync_notifications.build_payload(event: SyncStatusEvent, external_user_id: str | None) -> dict[str, Any]` y `sync_notifications.is_enabled() -> bool`. Task Celery `publish_sync_notification(event_json: str)`. El fixture `tests/fixtures/sync_completed_v1.json` es el contrato canónico y se copia tal cual al monorepo en Task 7.

- [ ] **Step 1: Escribir el test que falla**

```python
# backend/tests/services/test_sync_notifications.py
import json
from datetime import datetime, timezone
from uuid import UUID

from app.schemas.sync_status import SyncSource, SyncStage, SyncStatus, SyncStatusEvent
from app.services.outgoing_webhooks import sync_notifications

USER = UUID("11111111-1111-1111-1111-111111111111")


def _event(**overrides) -> SyncStatusEvent:
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


def test_payload_matches_the_v1_contract():
    payload = sync_notifications.build_payload(_event(), external_user_id="uw-123")

    with open("tests/fixtures/sync_completed_v1.json") as handle:
        expected = json.load(handle)

    assert payload == expected


def test_payload_never_carries_health_data():
    """El evento dice qué cambió y dónde buscarlo. Nunca el valor."""
    payload = sync_notifications.build_payload(_event(), external_user_id="uw-123")
    serialised = json.dumps(payload)

    for forbidden in ("samples", "value", "values", "data"):
        assert forbidden not in payload
    assert "1240" not in serialised.replace('"records": 1240', "")


def test_payload_tolerates_a_missing_window():
    payload = sync_notifications.build_payload(
        _event(metadata={"types": []}), external_user_id="uw-123"
    )

    assert payload["earliest_record_start_at"] is None
    assert payload["latest_record_end_at"] is None


def test_payload_tolerates_a_missing_external_user_id():
    payload = sync_notifications.build_payload(_event(), external_user_id=None)

    assert payload["external_user_id"] is None
    assert payload["user_id"] == str(USER)
```

Y el fixture:

```json
{
  "event_type": "sync.completed",
  "schema_version": 1,
  "user_id": "11111111-1111-1111-1111-111111111111",
  "external_user_id": "uw-123",
  "provider": "apple",
  "batch_id": "b1",
  "occurred_at": "2026-09-11T14:03:12+00:00",
  "metrics": ["steps", "heart_rate"],
  "activity_types": ["sleep", "workout"],
  "earliest_record_start_at": "2026-09-10T22:14:00+00:00",
  "latest_record_end_at": "2026-09-11T13:58:00+00:00",
  "counts": {"records": 1240, "workouts": 2, "sleep": 1}
}
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `cd backend && uv run pytest tests/services/test_sync_notifications.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.outgoing_webhooks.sync_notifications`.

- [ ] **Step 3: Settings**

En `app/config.py`, junto a los flags de Svix:

```python
# Notificación de corrida terminada hacia el bus de AWS. Independiente de Svix:
# los dos pueden estar prendidos a la vez sin pisarse.
sync_notifications_enabled: bool = False
aws_sync_events_topic_arn: str | None = None
```

- [ ] **Step 4: Escribir el publisher**

```python
# backend/app/services/outgoing_webhooks/sync_notifications.py
"""Notificación de corrida de ingesta terminada hacia SNS.

El evento dice qué cambió y en qué ventana. **Nunca lleva dato de salud**:
el consumidor resuelve el detalle por la External API. Ver la sección 4 del
diseño 2026-09-11.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.config import settings
from app.schemas.sync_status import SyncStatusEvent

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Slugs de series que representan una actividad y no una métrica continua.
# Se derivan de los conteos del batch, no de una lista de tipos.
_ACTIVITY_FROM_COUNT = {"workouts_saved": "workout", "sleep_saved": "sleep"}


def is_enabled() -> bool:
    return settings.sync_notifications_enabled and settings.aws_sync_events_topic_arn is not None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_payload(event: SyncStatusEvent, external_user_id: str | None) -> dict[str, Any]:
    """Construir el evento `sync.completed` v1 a partir de un evento terminal."""
    metadata = event.metadata or {}
    activity_types = sorted(
        marker for key, marker in _ACTIVITY_FROM_COUNT.items() if int(metadata.get(key) or 0) > 0
    )
    return {
        "event_type": "sync.completed",
        "schema_version": SCHEMA_VERSION,
        "user_id": str(event.user_id),
        "external_user_id": external_user_id,
        "provider": event.provider,
        "batch_id": event.run_id,
        "occurred_at": _iso(event.ended_at),
        "metrics": list(metadata.get("types") or []),
        "activity_types": activity_types,
        "earliest_record_start_at": _iso(metadata.get("window_start")),
        "latest_record_end_at": _iso(metadata.get("window_end")),
        "counts": {
            "records": int(metadata.get("records_saved") or 0),
            "workouts": int(metadata.get("workouts_saved") or 0),
            "sleep": int(metadata.get("sleep_saved") or 0),
        },
    }


def _create_sns_client() -> Any:
    """Cliente SNS con las credenciales de settings, o el rol de la task si no hay.

    Mismo patrón condicional que `raw_payload_storage._create_s3_client`: sin este
    condicional, correr con rol de task rompe.
    """
    try:
        import boto3

        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
        return boto3.client("sns", **kwargs)
    except Exception:
        logger.exception("Could not create the SNS client — sync notifications disabled")
        return None


def publish(payload: dict[str, Any]) -> bool:
    """Publicar en el topic FIFO. Devuelve False en vez de propagar."""
    client = _create_sns_client()
    if client is None:
        return False
    try:
        client.publish(
            TopicArn=settings.aws_sync_events_topic_arn,
            Message=json.dumps(payload),
            # Ordena por usuario: dos corridas del mismo usuario nunca se procesan
            # en paralelo del lado del consumidor (snapshot-replace).
            MessageGroupId=payload["external_user_id"] or payload["user_id"],
            MessageDeduplicationId=payload["batch_id"],
        )
        return True
    except Exception:
        logger.exception("Failed to publish sync.completed for user %s", payload["user_id"])
        return False
```

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/services/test_sync_notifications.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Task Celery que resuelve el `external_user_id`**

La resolución necesita base, y el camino de ingesta no puede pagar un roundtrip. Va a un task, igual que `emit_webhook_event`.

```python
# backend/app/integrations/celery/tasks/publish_sync_notification_task.py
"""Publicar el evento `sync.completed` fuera del camino de ingesta."""

from __future__ import annotations

from logging import getLogger
from typing import Any
from uuid import UUID

from celery import shared_task

from app.database import SessionLocal
from app.models import User
from app.repositories.user_repository import UserRepository
from app.schemas.sync_status import SyncStatusEvent
from app.services.outgoing_webhooks import sync_notifications

logger = getLogger(__name__)


@shared_task(
    name="app.integrations.celery.tasks.publish_sync_notification_task.publish_sync_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def publish_sync_notification(self: Any, event_json: str) -> dict[str, Any]:
    event = SyncStatusEvent.model_validate_json(event_json)

    with SessionLocal() as db:
        user = UserRepository(User).get(db, event.user_id)
        external_user_id = user.external_user_id if user else None

    payload = sync_notifications.build_payload(event, external_user_id)
    if not sync_notifications.publish(payload):
        raise self.retry(exc=RuntimeError(f"SNS publish failed for batch {payload['batch_id']}"))
    return {"batch_id": payload["batch_id"], "published": True}
```

Registrarlo en `app/integrations/celery/tasks/__init__.py` siguiendo el patrón de `emit_webhook_event`: import y entrada en `__all__`.

- [ ] **Step 7: Test del task**

```python
# backend/tests/tasks/test_publish_sync_notification.py
from unittest.mock import patch


def test_resolves_external_user_id_from_the_database(db_session, seeded_user_with_external_id, event_json):
    with patch("app.services.outgoing_webhooks.sync_notifications.publish", return_value=True) as mock_publish:
        publish_sync_notification(event_json)

    assert mock_publish.call_args[0][0]["external_user_id"] == seeded_user_with_external_id.external_user_id


def test_retries_when_the_publish_fails(event_json):
    with patch("app.services.outgoing_webhooks.sync_notifications.publish", return_value=False):
        with pytest.raises(Retry):
            publish_sync_notification(event_json)
```

Run: `cd backend && uv run pytest tests/tasks/test_publish_sync_notification.py -v`
Expected: PASS.

- [ ] **Step 8: Lint y commit**

```bash
cd backend && uv run pre-commit run --all-files
git add backend/app backend/tests
git commit -m "feat(sync): add the sync.completed SNS publisher and its Celery task"
```

---

### Task 3: Hook de emisión en `emit_event`

**Files:**
- Modify: `backend/app/services/sync_status_service.py:196-233`
- Test: `backend/tests/services/test_sync_status_emission_hook.py`

**Interfaces:**
- Consumes: `publish_sync_notification` de Task 2.
- Produces: nada nuevo. A partir de acá, **cualquier** camino de ingesta que llame `completed()` o `webhook_delivered()` con status exitoso emite el evento.

- [ ] **Step 1: Escribir el test que falla**

```python
# backend/tests/services/test_sync_status_emission_hook.py
from unittest.mock import patch

import pytest

from app.schemas.sync_status import SyncSource, SyncStage, SyncStatus
from app.services import sync_status_service

USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def dispatch():
    target = "app.integrations.celery.tasks.publish_sync_notification_task.publish_sync_notification.delay"
    with patch(target) as mock:
        yield mock


def test_emits_on_a_successful_terminal_event(dispatch, enabled_sync_notifications):
    sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert dispatch.call_count == 1


@pytest.mark.parametrize("stage", [SyncStage.STARTED, SyncStage.PROGRESS])
def test_does_not_emit_on_non_terminal_stages(dispatch, enabled_sync_notifications, stage):
    sync_status_service.emit_event(
        user_id=USER, provider="apple", source=SyncSource.SDK, stage=stage, status=SyncStatus.SUCCESS
    )

    assert dispatch.call_count == 0


@pytest.mark.parametrize("status", [SyncStatus.SKIPPED, SyncStatus.FAILED])
def test_does_not_emit_on_skipped_or_failed(dispatch, enabled_sync_notifications, status):
    """`webhook_delivered` usa SKIPPED para entregas duplicadas y no-op."""
    sync_status_service.emit_event(
        user_id=USER, provider="whoop", source=SyncSource.WEBHOOK,
        stage=SyncStage.COMPLETED, status=status,
    )

    assert dispatch.call_count == 0


def test_does_not_emit_when_the_flag_is_off(dispatch):
    sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert dispatch.call_count == 0


def test_a_broker_failure_never_breaks_ingestion(dispatch, enabled_sync_notifications):
    """La ingesta nunca puede fallar porque el bus esté caído."""
    dispatch.side_effect = ConnectionError("redis down")

    event = sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert event.run_id == "b1"
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `cd backend && uv run pytest tests/services/test_sync_status_emission_hook.py -v`
Expected: FAIL — `dispatch.call_count == 0` en el primer test.

- [ ] **Step 3: Escribir el hook**

En `sync_status_service.py`, antes de `emit_event`:

```python
# Estados terminales que representan "entró dato". SKIPPED queda afuera a
# propósito: `webhook_delivered` lo usa para entregas duplicadas y no-op.
_NOTIFIABLE_STATUSES = frozenset({SyncStatus.SUCCESS, SyncStatus.PARTIAL})


def _notify_sync_completed(event: SyncStatusEvent) -> None:
    """Publicar el evento de corrida terminada, sin poder romper la ingesta.

    Este es el único productor de `sync.completed`, y cubre los cuatro caminos
    de ingesta del fork porque todos desembocan acá. Ver la sección 4 del
    diseño 2026-09-11.
    """
    if event.stage != SyncStage.COMPLETED or event.status not in _NOTIFIABLE_STATUSES:
        return

    from app.services.outgoing_webhooks import sync_notifications

    if not sync_notifications.is_enabled():
        return

    try:
        from app.integrations.celery.tasks.publish_sync_notification_task import publish_sync_notification

        publish_sync_notification.delay(event.model_dump_json())
    except Exception:
        logger.warning("Could not enqueue sync.completed for run %s", event.run_id, exc_info=True)
```

Y en `emit_event`, después de `emit(event)`:

```python
    emit(event)
    _notify_sync_completed(event)
    return event
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/services/test_sync_status_emission_hook.py -v`
Expected: PASS (7 tests, contando los parametrizados).

- [ ] **Step 5: Correr la suite completa**

Run: `cd backend && uv run pytest -q`
Expected: sin regresiones. `emit_event` está en el camino de muchos tests; si alguno rompe, casi seguro es que el flag quedó prendido en un fixture.

- [ ] **Step 6: Lint y commit**

```bash
cd backend && uv run pre-commit run --all-files
git add backend/app backend/tests
git commit -m "feat(sync): emit sync.completed from the shared terminal-event seam"
```

---

## Fase B — Puente (repo `longevoIac`)

### Task 4: Topic SNS FIFO

**Files:**
- Create: `src/open-wearables/environments/qa/sync-events.tf`

**Interfaces:**
- Consumes: el rol de task del worker, ya definido en `environments/qa/`.
- Produces: topic FIFO `qa-open-wearables-sync-events.fifo`. Su nombre es determinista y es lo que Task 5 resuelve por `data`.

- [ ] **Step 1: Escribir el archivo**

```hcl
# Topic de notificaciones de corrida terminada. El evento no lleva PHI, pero
# el topic va cifrado igual: es el mismo dato de "quién sincronizó y cuándo".
#
# La cola que lo consume vive en el proyecto del consumidor
# (src/longevo/wearables), siguiendo el reparto de modules/proprietary-wearable-raw-ingest.

resource "aws_sns_topic" "sync_events" {
  name                        = "${local.environment}-${local.project}-sync-events.fifo"
  fifo_topic                  = true
  content_based_deduplication = false # el productor manda MessageDeduplicationId = batch_id
  kms_master_key_id           = aws_kms_key.main.id
}

data "aws_iam_policy_document" "sync_events_publish" {
  statement {
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.sync_events.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "worker_sync_events_publish" {
  name   = "${local.environment}-${local.project}-sync-events-publish"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.sync_events_publish.json
}

output "sync_events_topic_arn" {
  value       = aws_sns_topic.sync_events.arn
  description = "ARN del topic de sync.completed. Se inyecta como AWS_SYNC_EVENTS_TOPIC_ARN."
}
```

- [ ] **Step 2: Inyectar el ARN y el flag en las task definitions**

En el archivo que define las variables de entorno de los servicios `api`, `celery-worker` y `celery-worker-bulk`, agregar:

```hcl
{ name = "SYNC_NOTIFICATIONS_ENABLED", value = "true" },
{ name = "AWS_SYNC_EVENTS_TOPIC_ARN", value = aws_sns_topic.sync_events.arn },
```

- [ ] **Step 3: Verificar el plan**

Run: `cd src/open-wearables/environments/qa && terraform init && terraform plan`
Expected: sólo recursos nuevos — un `aws_sns_topic`, un `aws_iam_role_policy` y las task definitions con dos variables más. Ningún `destroy`.

Confirmar en la salida que los nombres de `aws_iam_role.task` y `aws_kms_key.main` coinciden con los reales del ambiente; si difieren, ajustar las referencias antes de aplicar.

- [ ] **Step 4: Aplicar y verificar**

```bash
cd src/open-wearables/environments/qa && terraform apply
aws sns get-topic-attributes --topic-arn "$(terraform output -raw sync_events_topic_arn)" --region us-west-2
```
Expected: `FifoTopic: true` y `KmsMasterKeyId` presente.

- [ ] **Step 5: Commit**

```bash
git add src/open-wearables/environments/qa
git commit -m "feat(open-wearables): add the sync.completed FIFO topic and publish policy"
```

---

### Task 5: Cola SQS FIFO, suscripción y permisos de consumo

**Files:**
- Create: `src/longevo/wearables/environments/qa/open-wearables-sync-events.tf`

**Interfaces:**
- Consumes: el topic de Task 4, resuelto por `data "aws_sns_topic"`.
- Produces: cola `qa-wearables-open-wearables-sync-events.fifo` con su DLQ. Su URL es la variable `OPENWEARABLES_SYNC_QUEUE_URL` que consume Task 7.

- [ ] **Step 1: Escribir el archivo**

```hcl
# Puente de transporte entre Open Wearables y el dominio de Longevo.
#
# La cola vive acá y no en el proyecto productor, siguiendo el reparto de
# modules/proprietary-wearable-raw-ingest: el consumidor posee la cola aunque
# el productor sea otro sistema. La suscripción va con la cola, así dar de baja
# el consumidor es una operación de un solo proyecto.
#
# El topic se resuelve por data block y no por estado remoto: un rename falla
# en plan y no en apply (mismo criterio que src/longo/environments/prod/data.tf).

data "aws_sns_topic" "open_wearables_sync_events" {
  name = "qa-open-wearables-sync-events.fifo"
}

module "open_wearables_sync_events" {
  source = "../../../../modules/sqs"

  name        = "open-wearables-sync-events"
  project     = var.project
  environment = var.environment

  # FIFO: serializa por usuario vía MessageGroupId. Lo necesita el
  # snapshot-replace por ventana del writer de sueño.
  fifo_queue = true

  # El consumidor hace hasta tres pulls contra la External API por mensaje.
  visibility_timeout_seconds = 300
}

data "aws_iam_policy_document" "sync_events_send" {
  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [module.open_wearables_sync_events.queue_arn]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [data.aws_sns_topic.open_wearables_sync_events.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "sync_events_send" {
  queue_url = module.open_wearables_sync_events.queue_url
  policy    = data.aws_iam_policy_document.sync_events_send.json
}

resource "aws_sns_topic_subscription" "sync_events" {
  topic_arn = data.aws_sns_topic.open_wearables_sync_events.arn
  protocol  = "sqs"
  endpoint  = module.open_wearables_sync_events.queue_arn

  # El consumidor espera el evento como cuerpo del mensaje, no envuelto en la
  # notificación de SNS.
  raw_message_delivery = true

  depends_on = [aws_sqs_queue_policy.sync_events_send]
}

data "aws_iam_policy_document" "sync_events_consume" {
  statement {
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [module.open_wearables_sync_events.queue_arn]
  }
}

resource "aws_iam_role_policy" "wearables_api_sync_events_consume" {
  name   = "${var.environment}-${var.project}-open-wearables-sync-consume"
  role   = data.aws_iam_role.wearables_api_task.id
  policy = data.aws_iam_policy_document.sync_events_consume.json
}

output "open_wearables_sync_queue_url" {
  value       = module.open_wearables_sync_events.queue_url
  description = "Se inyecta en wearables-api como OPENWEARABLES_SYNC_QUEUE_URL."
}
```

- [ ] **Step 2: Resolver el rol de task de wearables-api**

Si `data.aws_iam_role.wearables_api_task` no existe en el ambiente, agregarlo en el archivo de datos del proyecto, con el nombre real del rol del servicio. Si el rol se declara como recurso en este mismo proyecto, reemplazar el `data` por la referencia directa al recurso.

- [ ] **Step 3: Verificar el plan**

Run: `cd src/longevo/wearables/environments/qa && terraform init && terraform plan`
Expected: cola, DLQ, queue policy, suscripción y policy de rol. Ningún `destroy`. Si el `data "aws_sns_topic"` falla, es que Task 4 no se aplicó todavía — ése es exactamente el fallo temprano que buscábamos.

- [ ] **Step 4: Aplicar y probar el puente de punta a punta**

```bash
cd src/longevo/wearables/environments/qa && terraform apply
```

Prueba manual, con el topic de Task 4:

```bash
aws sns publish --region us-west-2 \
  --topic-arn "arn:aws:sns:us-west-2:577082859150:qa-open-wearables-sync-events.fifo" \
  --message '{"event_type":"sync.completed","schema_version":1,"user_id":"11111111-1111-1111-1111-111111111111","external_user_id":"smoke-test","provider":"apple","batch_id":"smoke-1","occurred_at":"2026-09-11T14:03:12+00:00","metrics":[],"activity_types":[],"earliest_record_start_at":null,"latest_record_end_at":null,"counts":{"records":0,"workouts":0,"sleep":0}}' \
  --message-group-id smoke-test --message-deduplication-id smoke-1
```

```bash
aws sqs receive-message --region us-west-2 --queue-url "$(terraform output -raw open_wearables_sync_queue_url)" --max-number-of-messages 1
```

Expected: el cuerpo es el JSON tal cual, sin el envoltorio de SNS (eso valida `raw_message_delivery`). Borrar el mensaje después con `delete-message`.

- [ ] **Step 5: Secreto de la API key de lectura**

El pull de Task 10 necesita una API key de Open Wearables. Crearla en el panel de QA, guardarla como secreto e inyectarla junto a la URL de la cola en la task definition de `wearables-api`:

```hcl
resource "aws_secretsmanager_secret" "open_wearables_api_key" {
  name        = "${var.environment}/${var.project}/open-wearables-api-key"
  description = "API key de lectura contra la External API de Open Wearables. La escritura la bloquea el WAF (Task 6)."
}
```

Variables de entorno del servicio:

```hcl
{ name = "OPENWEARABLES_SYNC_QUEUE_URL", value = module.open_wearables_sync_events.queue_url },
{ name = "OPENWEARABLES_SYNC_QUEUE_NAME", value = "${var.environment}-${var.project}-open-wearables-sync-events.fifo" },
{ name = "OPENWEARABLES_HOST", value = "https://<api-host-de-qa>" },
```

Y la key por `secrets`, no por `environment`, para que no quede en la task definition.

- [ ] **Step 6: Alarma de eventos descartados**

Un camino de ingesta cuyo `metadata` no propaga la ventana deja de llegar al dominio **en silencio**. Es la contracara de descartar en vez de puleear a ciegas, y por eso es alarma y no sólo log. El metric filter engancha la línea que emite el handler de Task 11:

```hcl
resource "aws_cloudwatch_log_metric_filter" "sync_events_discarded" {
  name           = "${var.environment}-${var.project}-sync-events-discarded"
  log_group_name = data.aws_cloudwatch_log_group.wearables_api.name
  pattern        = "openwearables.sync_event.discarded"

  metric_transformation {
    name      = "OpenWearablesSyncEventsDiscarded"
    namespace = "Longevo/Wearables"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "sync_events_discarded" {
  alarm_name          = "${var.environment}-${var.project}-sync-events-discarded"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "Longevo/Wearables"
  metric_name         = "OpenWearablesSyncEventsDiscarded"
  treat_missing_data  = "notBreaching"
  alarm_description   = "Eventos de Open Wearables descartados. Casi siempre significa que un camino de ingesta se habilitó sin propagar la ventana temporal."
}
```

Y una alarma sobre la DLQ, que la sección 11 del spec ya pedía:

```hcl
resource "aws_cloudwatch_metric_alarm" "sync_events_dlq" {
  alarm_name          = "${var.environment}-${var.project}-sync-events-dlq"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = "${var.environment}-${var.project}-open-wearables-sync-events-dlq.fifo"
  }
}
```

Confirmar el nombre real de la DLQ contra `modules/sqs/main.tf` antes de aplicar.

- [ ] **Step 7: Commit**

```bash
git add src/longevo/wearables/environments/qa
git commit -m "feat(wearables): add the open-wearables sync events FIFO queue, subscription and alarms"
```

---

### Task 6: Regla de WAF por método y path

**Files:**
- Modify: `src/open-wearables/environments/qa/dns-alb-waf.tf`

**Interfaces:**
- Consumes: la Web ACL existente.
- Produces: la garantía de que la API key de Longevo no puede escribir, sin tocar el fork.

- [ ] **Step 1: Agregar la regla**

Insertar con una prioridad **menor que la de las managed rules** (se evalúa antes), y ajustar los números si colisionan con los existentes:

```hcl
# Reemplazo del scope de la API key mientras el fork no tenga keys de sólo
# lectura. Válido porque en fase 1 Longevo es el único consumidor de la
# External API. Ver la sección 6 del diseño 2026-09-11.
rule {
  name     = "external-api-read-only"
  priority = 5

  action {
    block {}
  }

  statement {
    and_statement {
      # Cualquier path de lectura de la External API...
      statement {
        byte_match_statement {
          search_string         = "/api/v1/users/"
          positional_constraint = "STARTS_WITH"

          field_to_match {
            uri_path {}
          }

          text_transformation {
            priority = 0
            type     = "LOWERCASE"
          }
        }
      }

      # ...con cualquier método que no sea GET.
      statement {
        not_statement {
          statement {
            byte_match_statement {
              search_string         = "get"
              positional_constraint = "EXACTLY"

              field_to_match {
                method {}
              }

              text_transformation {
                priority = 0
                type     = "LOWERCASE"
              }
            }
          }
        }
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "ExternalApiReadOnly"
    sampled_requests_enabled   = true
  }
}
```

- [ ] **Step 2: Verificar que no bloquea la ingesta**

El SDK postea a `/api/v1/sdk/users/{id}/sync`, que **no** empieza con `/api/v1/users/`, así que queda fuera de la regla. Confirmarlo leyendo `backend/app/api/routes/v1/__init__.py` antes de aplicar: si algún router de ingesta cuelga de `/users/`, la regla lo rompe y hay que acotarla más.

- [ ] **Step 3: Aplicar y probar los dos lados**

```bash
cd src/open-wearables/environments/qa && terraform apply
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://<api-host>/api/v1/users" -H "X-Open-Wearables-API-Key: <key-de-qa>" -d '{}'
```
Expected: `403`.

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://<api-host>/api/v1/users/<uuid>/summaries/activity?start_date=2026-09-01&end_date=2026-09-02" -H "X-Open-Wearables-API-Key: <key-de-qa>"
```
Expected: `200`.

Y una prueba de ingesta real desde la app en QA, para confirmar que el sync sigue entrando.

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/dns-alb-waf.tf
git commit -m "feat(open-wearables): block non-GET on the external API at the edge"
```

---

## Fase C — Consumidor (repo `monorepo-backend`, rama `feat/openwearables-integration`)

### Task 7: Contrato del evento del lado del consumidor

**Files:**
- Create: `apps/wearables-api/src/dtos/openwearables-sync-event.ts`
- Create: `apps/wearables-api/test/fixtures/sync-completed-v1.json`
- Modify: `apps/wearables-api/src/dtos/index.ts`
- Test: `apps/wearables-api/src/dtos/__tests__/openwearables-sync-event.spec.ts`

**Interfaces:**
- Consumes: el fixture de Task 2, copiado byte a byte.
- Produces: `OpenWearablesSyncEventRaw` (forma del cable), `OpenWearablesSyncEvent` (forma del dominio, con `userId`, `externalUserId`, `provider`, `batchId`, `metrics`, `activityTypes`, `earliestRecordStartAt: Date`, `latestRecordEndAt: Date`) y `parseSyncEvent(raw) -> {ok: true, event} | {ok: false, reason}`. Task 10 consume el tipo del dominio y Task 11 la función.

**Es una función pura a propósito.** Toda la política de descarte vive acá y se testea sin Nest, sin SQS y sin base.

- [ ] **Step 1: Copiar el fixture del fork**

```bash
cp ../longevo-wearables/backend/tests/fixtures/sync_completed_v1.json \
   apps/wearables-api/test/fixtures/sync-completed-v1.json
```

El fixture es el contrato. Si los dos archivos divergen, el productor y el consumidor divergieron.

- [ ] **Step 2: Escribir el test que falla**

```typescript
// apps/wearables-api/src/dtos/__tests__/openwearables-sync-event.spec.ts
import { parseSyncEvent } from '../openwearables-sync-event';

import fixture from '../../../test/fixtures/sync-completed-v1.json';

describe('parseSyncEvent', () => {
  it('parses the v1 contract fixture into the domain shape', () => {
    const result = parseSyncEvent(fixture);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.event.externalUserId).toBe(fixture.external_user_id);
    expect(result.event.userId).toBe(fixture.user_id);
    expect(result.event.earliestRecordStartAt).toEqual(new Date(fixture.earliest_record_start_at));
    expect(result.event.latestRecordEndAt).toEqual(new Date(fixture.latest_record_end_at));
  });

  it('rejects an event with no window instead of pulling blind', () => {
    const result = parseSyncEvent({
      ...fixture,
      earliest_record_start_at: null,
      latest_record_end_at: null,
    });

    expect(result).toEqual({ ok: false, reason: 'window' });
  });

  it('rejects an event with no external_user_id', () => {
    expect(parseSyncEvent({ ...fixture, external_user_id: null })).toEqual({
      ok: false,
      reason: 'external_user_id',
    });
  });

  it('rejects an unknown schema_version rather than guessing', () => {
    expect(parseSyncEvent({ ...fixture, schema_version: 2 })).toEqual({
      ok: false,
      reason: 'schema_version',
    });
  });
});
```

- [ ] **Step 3: Correr el test y verificar que falla**

Run: `cd monorepo-backend && npx jest apps/wearables-api/src/dtos/__tests__/openwearables-sync-event.spec.ts`
Expected: FAIL — no existe el módulo.

- [ ] **Step 4: Escribir el DTO**

```typescript
// apps/wearables-api/src/dtos/openwearables-sync-event.ts
export const OPEN_WEARABLES_SYNC_SCHEMA_VERSION = 1;

/** Contrato `sync.completed` v1. Espejo de tests/fixtures/sync_completed_v1.json del fork. */
export interface OpenWearablesSyncEventRaw {
  event_type: string;
  schema_version: number;
  user_id: string;
  external_user_id: string | null;
  provider: string;
  batch_id: string;
  occurred_at: string;
  metrics: string[];
  activity_types: string[];
  earliest_record_start_at: string | null;
  latest_record_end_at: string | null;
  counts: { records: number; workouts: number; sleep: number };
}

export interface OpenWearablesSyncEvent {
  userId: string;
  externalUserId: string;
  provider: string;
  batchId: string;
  metrics: string[];
  activityTypes: string[];
  earliestRecordStartAt: Date;
  latestRecordEndAt: Date;
}

export type SyncEventRejection = 'schema_version' | 'external_user_id' | 'window';

/**
 * Traduce el evento crudo al del dominio, o dice por qué no se puede.
 *
 * No hay fallback de ventana a propósito: puleear sin ventana contra un usuario
 * con años de historia es un incidente, no un valor por defecto.
 */
export function parseSyncEvent(
  raw: OpenWearablesSyncEventRaw,
): { ok: true; event: OpenWearablesSyncEvent } | { ok: false; reason: SyncEventRejection } {
  if (raw.schema_version !== OPEN_WEARABLES_SYNC_SCHEMA_VERSION) {
    return { ok: false, reason: 'schema_version' };
  }
  if (!raw.external_user_id) {
    return { ok: false, reason: 'external_user_id' };
  }
  if (!raw.earliest_record_start_at || !raw.latest_record_end_at) {
    return { ok: false, reason: 'window' };
  }

  return {
    ok: true,
    event: {
      userId: raw.user_id,
      externalUserId: raw.external_user_id,
      provider: raw.provider,
      batchId: raw.batch_id,
      metrics: raw.metrics ?? [],
      activityTypes: raw.activity_types ?? [],
      earliestRecordStartAt: new Date(raw.earliest_record_start_at),
      latestRecordEndAt: new Date(raw.latest_record_end_at),
    },
  };
}
```

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd monorepo-backend && npx jest apps/wearables-api/src/dtos/__tests__/openwearables-sync-event.spec.ts`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint y commit**

```bash
cd monorepo-backend && pnpm run lint:fix && pnpm run format
git add apps/wearables-api/src/dtos apps/wearables-api/test/fixtures
git commit -m "feat(wearables-api): add the open-wearables sync.completed v1 contract"
```

---

### Task 8: Writer de sueño con snapshot-replace

**Files:**
- Create: `libs/wearables-v2-sdk/src/infraestructure/openwearables-sleep-writer.service.ts`
- Modify: `libs/wearables-v2-sdk/src/index.ts`, `wearables-v2-sdk.module.ts`
- Test: `libs/wearables-v2-sdk/src/__tests__/openwearables-sleep-writer.service.spec.ts`

**Interfaces:**
- Consumes: el modelo Mongoose de sueño que ya usa `SpikeSleepV2WriterService`.
- Produces: `OpenWearablesSleepWriterService.replaceWindow(params: { sleeps: OpenWearablesSleepSession[]; userWearableId: string; applicationId: number; isSuraIntegration: boolean; from: Date; to: Date }): Promise<SpikeSleepRecordEntity[]>`.

**Por qué existe:** el ID de sueño de Open Wearables **no es estable**. `finish_sleep` borra el registro adyacente y crea uno nuevo con `uuid4()` cada vez que una noche se extiende, y ése es el patrón normal del SDK de Apple. Un writer que hiciera upsert por id crearía un documento por payload. Ver la sección 7 del diseño.

- [ ] **Step 1: Escribir los tests que fallan**

```typescript
// libs/wearables-v2-sdk/src/__tests__/openwearables-sleep-writer.service.spec.ts
describe('OpenWearablesSleepWriterService', () => {
  const sleep = (id: string, date: string, startIso: string, endIso: string) => ({
    id,
    start_time: startIso,
    end_time: endIso,
    duration_seconds: 28800,
    is_nap: false,
    source: { provider: 'apple' },
    sleep_date: date,
  });

  it('la noche fragmentada termina como un solo documento', async () => {
    // Seis payloads de una misma noche: Open Wearables devuelve un UUID
    // distinto en cada pull porque fusiona y recrea el registro.
    for (let attempt = 1; attempt <= 6; attempt += 1) {
      await writer.replaceWindow({
        sleeps: [sleep(`uuid-${attempt}`, '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z')],
        userWearableId: 'uw-1',
        applicationId: 2,
        isSuraIntegration: false,
        from: new Date('2026-09-10T00:00:00Z'),
        to: new Date('2026-09-11T23:59:59Z'),
      });
    }

    const stored = await model.find({ user_wearable_id: 'uw-1' }).exec();
    expect(stored).toHaveLength(1);
    expect(stored[0].sleep_id).toBe('uuid-6');
  });

  it('replicar un borrado: la ventana vuelve vacía y el documento desaparece', async () => {
    await writer.replaceWindow({ sleeps: [sleep('uuid-1', '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z')], ...base });

    await writer.replaceWindow({ sleeps: [], ...base });

    expect(await model.find({ user_wearable_id: 'uw-1' }).exec()).toHaveLength(0);
  });

  it('no toca los sueños de fuera de la ventana', async () => {
    await writer.replaceWindow({ sleeps: [sleep('viejo', '2026-09-01', '2026-08-31T23:00:00Z', '2026-09-01T07:00:00Z')], ...baseFor('2026-09-01') });

    await writer.replaceWindow({ sleeps: [sleep('nuevo', '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z')], ...baseFor('2026-09-11') });

    const stored = await model.find({ user_wearable_id: 'uw-1' }).exec();
    expect(stored.map(d => d.sleep_id).sort()).toEqual(['nuevo', 'viejo']);
  });

  it('no toca los sueños de otro usuario en la misma ventana', async () => {
    await writer.replaceWindow({ sleeps: [sleep('de-otro', '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z')], ...base, userWearableId: 'uw-2' });

    await writer.replaceWindow({ sleeps: [sleep('mio', '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z')], ...base });

    expect(await model.find({ user_wearable_id: 'uw-2' }).exec()).toHaveLength(1);
  });

  it('conserva las siestas del mismo día', async () => {
    const nap = { ...sleep('siesta', '2026-09-11', '2026-09-11T14:00:00Z', '2026-09-11T14:40:00Z'), is_nap: true };

    await writer.replaceWindow({ sleeps: [sleep('noche', '2026-09-11', '2026-09-10T23:00:00Z', '2026-09-11T07:00:00Z'), nap], ...base });

    expect(await model.find({ user_wearable_id: 'uw-1' }).exec()).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd monorepo-backend && npx jest libs/wearables-v2-sdk/src/__tests__/openwearables-sleep-writer.service.spec.ts`
Expected: FAIL — no existe el servicio.

- [ ] **Step 3: Escribir el writer**

```typescript
// libs/wearables-v2-sdk/src/infraestructure/openwearables-sleep-writer.service.ts
import { Injectable, Logger } from '@nestjs/common';
import { InjectModel } from '@nestjs/mongoose';
import { Model } from 'mongoose';
import { DateTime } from 'luxon';

/**
 * Escribe el sueño de Open Wearables reemplazando la ventana completa.
 *
 * No hace upsert por id porque el id de Open Wearables no es estable:
 * `finish_sleep` borra el registro adyacente y crea uno nuevo con uuid4() cada
 * vez que una noche se extiende con un payload más, que es el patrón normal
 * del SDK de Apple. Un upsert por id dejaría un documento por payload.
 *
 * Reemplazar la ventana además replica los borrados del provider sin necesidad
 * de un tipo de evento aparte: si la ventana vuelve sin el registro, desaparece.
 */
@Injectable()
export class OpenWearablesSleepWriterService {
  private readonly logger = new Logger(OpenWearablesSleepWriterService.name);

  constructor(
    @InjectModel(SpikeSleepRecord.name)
    private readonly model: Model<SpikeSleepRecordDocument>,
  ) {}

  async replaceWindow(params: ReplaceSleepWindowParams): Promise<SpikeSleepRecordEntity[]> {
    const { sleeps, userWearableId, applicationId, isSuraIntegration, from, to } = params;

    const scope = {
      user_wearable_id: userWearableId,
      application_id: applicationId,
      is_sura_integration: isSuraIntegration,
      start_at_timestamp: { $gte: from, $lte: to },
    };

    const documents = sleeps.map(item => ({
      sleep_id: item.id,
      sleep_date: item.sleep_date ?? DateTime.fromISO(item.start_time, { zone: 'utc' }).toISODate(),
      start_at_timestamp: DateTime.fromISO(item.start_time, { zone: 'utc' }).toJSDate(),
      end_at_timestamp: item.end_time
        ? DateTime.fromISO(item.end_time, { zone: 'utc' }).toJSDate()
        : undefined,
      duration: item.duration_seconds != null ? item.duration_seconds * 1000 : undefined,
      score: item.efficiency_percent ?? null,
      stages: item.sleep_stage_intervals ?? [],
      metrics: item.stages ?? {},
      provider_slug: item.source?.provider ?? null,
      is_nap: item.is_nap ?? false,
      user_wearable_id: userWearableId,
      application_id: applicationId,
      is_sura_integration: isSuraIntegration,
      updated_at: new Date(),
      created_at: new Date(),
    }));

    // Borrar e insertar en ese orden: la ventana que devuelve la External API
    // es el estado autoritativo de esa ventana.
    await this.model.deleteMany(scope).exec();
    const inserted = documents.length > 0 ? await this.model.insertMany(documents) : [];

    this.logger.log(
      `openwearables.sleep.window_replaced user=${userWearableId} ` +
        `from=${from.toISOString()} to=${to.toISOString()} count=${inserted.length}`,
    );

    return inserted.map(document => this.mapDocumentToEntity(document));
  }
}
```

Reusar `mapDocumentToEntity` de `SpikeSleepV2WriterService`: si está privado, extraerlo a un módulo compartido en vez de duplicarlo.

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `cd monorepo-backend && npx jest libs/wearables-v2-sdk/src/__tests__/openwearables-sleep-writer.service.spec.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint y commit**

```bash
cd monorepo-backend && pnpm run lint:fix && pnpm run format
git add libs/wearables-v2-sdk
git commit -m "feat(wearables-v2): add a window snapshot-replace sleep writer for open wearables"
```

---

### Task 9: `OPEN_WEARABLES` como fuente y su predicado

**Files:**
- Modify: `libs/wearables-sdk/src/core/entities/wearable-source-type.ts`
- Modify: `libs/wearables-sdk/src/infraestructure/default-wearable-source.service.ts`
- Modify: `libs/wearables-sdk/src/infraestructure/sql-wearables-repository.service.ts`
- Test: `libs/wearables-sdk/src/__tests__/default-wearable-source.service.spec.ts`

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces: `WearableSourceType.OPEN_WEARABLES`. `SqlWearablesRepository.markOpenWearablesProducing(userId: string): Promise<void>` y `hasOpenWearablesProducing(userId: string): Promise<boolean>`, respaldados por una columna booleana nueva en `user_wearables`. Task 10 llama a la primera.

**Por qué un flag y no la existencia del `openWearablesUserId`:** `POST /session` es idempotente y mobile lo llama de forma recurrente. Que exista la sesión prueba que la sesión existe, no que haya dato. La marca se pone al procesar el **primer `sync.completed` exitoso**.

- [ ] **Step 1: Escribir los tests que fallan**

```typescript
describe('DefaultWearableSourceService con Open Wearables', () => {
  it('el wearable propietario le sigue ganando a Open Wearables', async () => {
    proprietaryRepo.hasActiveDevice.mockResolvedValue(true);
    wearablesRepo.hasOpenWearablesProducing.mockResolvedValue(true);

    expect(await service.getDefaultSource('u1')).toBe(WearableSourceType.PROPRIETARY);
  });

  it('Open Wearables le gana a Spike cuando ya está produciendo', async () => {
    proprietaryRepo.hasActiveDevice.mockResolvedValue(false);
    wearablesRepo.hasOpenWearablesProducing.mockResolvedValue(true);
    wearablesRepo.hasLiveIntegrationByUserId.mockResolvedValue(true);

    expect(await service.getDefaultSource('u1')).toBe(WearableSourceType.OPEN_WEARABLES);
  });

  it('una sesión sin dato todavía no cambia la fuente', async () => {
    proprietaryRepo.hasActiveDevice.mockResolvedValue(false);
    wearablesRepo.hasOpenWearablesProducing.mockResolvedValue(false);
    wearablesRepo.hasLiveIntegrationByUserId.mockResolvedValue(true);

    expect(await service.getDefaultSource('u1')).toBe(WearableSourceType.SPIKE);
  });

  it('marcar produciendo invalida el caché para que el switch se vea ya', async () => {
    await service.getDefaultSource('u1');
    await service.invalidate('u1');

    expect(cacheManager.del).toHaveBeenCalledWith('wearables:default-source:u1');
  });
});
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `cd monorepo-backend && npx jest libs/wearables-sdk/src/__tests__/default-wearable-source.service.spec.ts`
Expected: FAIL — `OPEN_WEARABLES` no existe en el enum.

- [ ] **Step 3: Enum y precedencia**

```typescript
// libs/wearables-sdk/src/core/entities/wearable-source-type.ts
/** Only the user's default source may publish user-action events (see WearableUserActionPublisherService). */
export enum WearableSourceType {
  SPIKE = 'SPIKE',
  PROPRIETARY = 'PROPRIETARY',
  OPEN_WEARABLES = 'OPEN_WEARABLES',
}
```

```typescript
// default-wearable-source.service.ts
private async resolve(userId: string): Promise<WearableSourceType | null> {
  if (await this.proprietaryDeviceRepository.hasActiveDevice(userId)) {
    return WearableSourceType.PROPRIETARY;
  }

  // Open Wearables gana sobre Spike en cuanto produce dato. El predicado es
  // "ya llegó un sync.completed", no "existe la sesión": POST /session es
  // idempotente y no prueba que haya dato.
  if (await this.wearablesRepository.hasOpenWearablesProducing(userId)) {
    return WearableSourceType.OPEN_WEARABLES;
  }

  if (await this.wearablesRepository.hasLiveIntegrationByUserId(userId)) {
    return WearableSourceType.SPIKE;
  }

  return null;
}
```

Ampliar también el `if` de lectura del caché para aceptar el valor nuevo:

```typescript
if (
  cached === WearableSourceType.SPIKE ||
  cached === WearableSourceType.PROPRIETARY ||
  cached === WearableSourceType.OPEN_WEARABLES
) {
  return cached;
}
```

- [ ] **Step 4: Columna y métodos del repositorio**

Agregar `openWearablesProducingAt: Date | null` a la entidad `user_wearables` con su migración **aditiva**, y en `SqlWearablesRepository`:

```typescript
async hasOpenWearablesProducing(userId: string): Promise<boolean> {
  const row = await this.repository.findOne({
    where: { userId },
    select: ['openWearablesProducingAt'],
  });
  return row?.openWearablesProducingAt != null;
}

/** Idempotente: sólo escribe la primera vez, para conservar cuándo migró el usuario. */
async markOpenWearablesProducing(userId: string): Promise<void> {
  await this.repository.update(
    { userId, openWearablesProducingAt: IsNull() },
    { openWearablesProducingAt: new Date() },
  );
}
```

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd monorepo-backend && npx jest libs/wearables-sdk`
Expected: PASS, y sin regresiones en los tests existentes de la fuente default.

- [ ] **Step 6: Lint y commit**

```bash
cd monorepo-backend && pnpm run lint:fix && pnpm run format
git add libs/wearables-sdk
git commit -m "feat(wearables-sdk): add OPEN_WEARABLES as a default source, gated on first data"
```

---

### Task 10: Use case de sincronización

**Files:**
- Create: `apps/wearables-api/src/use-cases/handle-openwearables-sync.use-case.ts`
- Create: `apps/wearables-api/src/mappers/openwearables.mapper.ts`
- Modify: `apps/wearables-api/src/use-cases/index.ts`, `src/wearables-api.module.ts`
- Test: `apps/wearables-api/src/use-cases/__tests__/handle-openwearables-sync.use-case.spec.ts`

**Interfaces:**
- Consumes: `OpenWearablesSyncEvent` (Task 7), `OpenWearablesSleepWriterService.replaceWindow` (Task 8), `WearableSourceType.OPEN_WEARABLES` y `markOpenWearablesProducing` (Task 9), y `OpenWearablesSdkService` con `getActivitySummaries`, `getSleepSessions` y `getWorkouts`.
- Produces: `HandleOpenWearablesSyncUseCase.execute(event: OpenWearablesSyncEvent): Promise<void>`.

- [ ] **Step 1: Escribir los tests que fallan**

```typescript
describe('HandleOpenWearablesSyncUseCase', () => {
  it('marca al usuario como produciendo e invalida el caché de fuente', async () => {
    await useCase.execute(event);

    expect(wearablesRepo.markOpenWearablesProducing).toHaveBeenCalledWith('user-1');
    expect(defaultSourceService.invalidate).toHaveBeenCalledWith('user-1');
  });

  it('no escribe si Open Wearables no es la fuente default del usuario', async () => {
    // Durante el solapamiento con Spike, una sola fuente escribe por usuario.
    defaultSourceService.getDefaultSource.mockResolvedValue(WearableSourceType.PROPRIETARY);

    await useCase.execute(event);

    expect(sleepWriter.replaceWindow).not.toHaveBeenCalled();
    expect(workoutWriter.upsert).not.toHaveBeenCalled();
  });

  it('sólo pulea sueño cuando el evento trae sueño', async () => {
    await useCase.execute({ ...event, activityTypes: ['workout'], metrics: [] });

    expect(sdk.getSleepSessions).not.toHaveBeenCalled();
    expect(sdk.getWorkouts).toHaveBeenCalledTimes(1);
  });

  it('sólo pulea resúmenes cuando el evento trae métricas', async () => {
    await useCase.execute({ ...event, activityTypes: ['sleep'], metrics: [] });

    expect(sdk.getActivitySummaries).not.toHaveBeenCalled();
  });

  it('pulea con la ventana del evento, no con un rango por defecto', async () => {
    await useCase.execute(event);

    expect(sdk.getSleepSessions).toHaveBeenCalledWith(
      'ow-uuid-1',
      expect.objectContaining({ start_date: '2026-09-10', end_date: '2026-09-11' }),
    );
  });

  it('reemplaza la ventana de sueño en vez de hacer upsert', async () => {
    await useCase.execute(event);

    expect(sleepWriter.replaceWindow).toHaveBeenCalledWith(
      expect.objectContaining({ from: event.earliestRecordStartAt, to: event.latestRecordEndAt }),
    );
  });

  it('publica el resumen del día después de escribir', async () => {
    await useCase.execute(event);

    const writeOrder = sleepWriter.replaceWindow.mock.invocationCallOrder[0];
    const publishOrder = publishDaySummary.execute.mock.invocationCallOrder[0];
    expect(writeOrder).toBeLessThan(publishOrder);
  });

  it('propaga el error del pull para que SQS reintente', async () => {
    sdk.getSleepSessions.mockRejectedValueOnce(new Error('502'));

    await expect(useCase.execute(event)).rejects.toThrow('502');
  });
});
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `cd monorepo-backend && npx jest apps/wearables-api/src/use-cases/__tests__/handle-openwearables-sync.use-case.spec.ts`
Expected: FAIL — no existe el use case.

- [ ] **Step 3: Escribir el use case**

```typescript
// apps/wearables-api/src/use-cases/handle-openwearables-sync.use-case.ts
import { Injectable, Logger } from '@nestjs/common';
import { DateTime } from 'luxon';

import { WearableSourceType } from '@app/wearables-sdk/core/entities/wearable-source-type';

import { OpenWearablesSyncEvent } from '../dtos/openwearables-sync-event';
import { getWearablesApplicationId } from '../helpers';

const SLEEP_ACTIVITY_TYPE = 'sleep';

@Injectable()
export class HandleOpenWearablesSyncUseCase {
  private readonly logger = new Logger(HandleOpenWearablesSyncUseCase.name);

  constructor(
    private readonly sdk: OpenWearablesSdkService,
    private readonly sleepWriter: OpenWearablesSleepWriterService,
    private readonly workoutWriter: SpikeWorkoutV2WriterService,
    private readonly dailyStatisticsWriter: SpikeDailyStatisticsV2WriterService,
    private readonly wearablesRepository: SqlWearablesRepository,
    private readonly defaultSourceService: DefaultWearableSourceService,
    private readonly userSdkService: UserSdkService,
    private readonly publishDaySummary: PublishWearableDaySummaryUseCase,
  ) {}

  async execute(event: OpenWearablesSyncEvent): Promise<void> {
    const userId = await this.userSdkService.findUserIdByUserWearableId(event.externalUserId);
    if (!userId) {
      this.logger.warn(`No Longevo user for wearable ${event.externalUserId} — dropping`);
      return;
    }

    // La marca es el hecho observable que gobierna las tres cosas: qué fuente
    // manda, quién puede escribir, y cuándo se da de baja Spike.
    await this.wearablesRepository.markOpenWearablesProducing(userId);
    await this.defaultSourceService.invalidate(userId);

    const defaultSource = await this.defaultSourceService.getDefaultSource(userId);
    if (defaultSource !== WearableSourceType.OPEN_WEARABLES) {
      this.logger.log(
        `Skipping write for ${userId}: default source is ${defaultSource}. ` +
          'One source writes per user during the Spike overlap.',
      );
      return;
    }

    const applicationId = getWearablesApplicationId(false);
    const fromDate = DateTime.fromJSDate(event.earliestRecordStartAt, { zone: 'utc' }).toISODate();
    const toDate = DateTime.fromJSDate(event.latestRecordEndAt, { zone: 'utc' }).toISODate();
    const scope = { userWearableId: event.externalUserId, applicationId, isSuraIntegration: false };
    const dates = new Set<string>();

    if (event.activityTypes.includes(SLEEP_ACTIVITY_TYPE)) {
      const sleeps = await this.sdk.getSleepSessions(event.userId, {
        start_date: fromDate,
        end_date: toDate,
        filter_by_priority: true,
      });
      // Reemplazo de ventana, no upsert: ver el writer y la sección 7 del diseño.
      const persisted = await this.sleepWriter.replaceWindow({
        ...scope,
        sleeps,
        from: event.earliestRecordStartAt,
        to: event.latestRecordEndAt,
      });
      persisted.forEach(item => dates.add(item.sleepDate));
    }

    if (event.metrics.length > 0) {
      const days = await this.sdk.getActivitySummaries(event.userId, {
        start_date: fromDate,
        end_date: toDate,
      });
      const persisted = await this.dailyStatisticsWriter.upsert({
        ...scope,
        days: days.map(mapOpenWearablesDayToSpikeDay),
        providerSlug: event.provider,
      });
      persisted.forEach(day => dates.add(day.date));
    }

    const hasWorkouts = event.activityTypes.some(type => type !== SLEEP_ACTIVITY_TYPE);
    if (hasWorkouts) {
      const workouts = await this.sdk.getWorkouts(event.userId, {
        start_date: fromDate,
        end_date: toDate,
      });
      if (workouts.length > 0) {
        await this.workoutWriter.upsert({ ...scope, workouts: workouts.map(mapOpenWearablesWorkout) });
      }
    }

    if (dates.size > 0) {
      await this.publishDaySummary.execute({
        userId,
        source: WearableSourceType.OPEN_WEARABLES,
        userWearableId: event.externalUserId,
        applicationId,
        isSuraIntegration: false,
        dates: [...dates].sort(),
      });
    }
  }
}
```

- [ ] **Step 4: Escribir los mappers**

`openwearables.mapper.ts` traduce `OpenWearablesActivitySummary` y `OpenWearablesWorkout` a las formas que ya consumen `SpikeDailyStatisticsV2WriterService` y `SpikeWorkoutV2WriterService`. Los campos de origen están en `libs/openwearables-sdk/src/core/entities/data.ts`; los de destino, en los writers correspondientes. Para workouts, **el `record_id` es el `id` de Open Wearables**: es estable porque el upsert de workouts del fork es `ON CONFLICT DO NOTHING` sobre `(data_source_id, start, end)`, a diferencia del de sueño.

- [ ] **Step 5: Aceptar `WearableSourceType.OPEN_WEARABLES` en el publisher de resúmenes**

`PublishWearableDaySummaryUseCase` hoy discrimina entre `SPIKE` y `PROPRIETARY`. Agregar la rama de Open Wearables reusando `buildSpikeSummaries`, que ya lee del store por `(userWearableId, applicationId, rango)` y no sabe de dónde vino el dato.

- [ ] **Step 6: Registrar y correr los tests**

Agregar `HandleOpenWearablesSyncUseCase` y `OpenWearablesSleepWriterService` a los `providers` del módulo.

Run: `cd monorepo-backend && npx jest apps/wearables-api`
Expected: PASS, sin regresiones en los tests de Spike.

- [ ] **Step 7: Lint y commit**

```bash
cd monorepo-backend && pnpm run lint:fix && pnpm run format
git add apps/wearables-api
git commit -m "feat(wearables-api): pull and persist open wearables data on sync.completed"
```

---

---

### Task 11: Handler de la cola y cableado

**Files:**
- Create: `apps/wearables-api/src/services/openwearables-sync-queue.handler.ts`
- Modify: `apps/wearables-api/src/wearables-api.module.ts:165-200` y `src/services/index.ts`
- Test: `apps/wearables-api/src/services/__tests__/openwearables-sync-queue.handler.spec.ts`

**Interfaces:**
- Consumes: `parseSyncEvent` y `OpenWearablesSyncEvent` (Task 7), `HandleOpenWearablesSyncUseCase.execute` (Task 10), y la variable `OPENWEARABLES_SYNC_QUEUE_URL` que produce Task 5.
- Produces: el consumidor vivo. Es la última pieza: al terminarla el circuito está cerrado.

- [ ] **Step 1: Escribir el test que falla**

```typescript
// apps/wearables-api/src/services/__tests__/openwearables-sync-queue.handler.spec.ts
import { Message } from '@aws-sdk/client-sqs';
import { Test, TestingModule } from '@nestjs/testing';

import { HandleOpenWearablesSyncUseCase } from '../../use-cases/handle-openwearables-sync.use-case';
import { OpenWearablesSyncQueueHandler } from '../openwearables-sync-queue.handler';

import fixture from '../../../test/fixtures/sync-completed-v1.json';

const asMessage = (body: unknown): Message => ({ MessageId: 'm1', Body: JSON.stringify(body) });

describe('OpenWearablesSyncQueueHandler', () => {
  let handler: OpenWearablesSyncQueueHandler;
  const mockUseCase = { execute: jest.fn() };

  beforeEach(async () => {
    jest.clearAllMocks();
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        OpenWearablesSyncQueueHandler,
        { provide: HandleOpenWearablesSyncUseCase, useValue: mockUseCase },
      ],
    }).compile();
    handler = module.get(OpenWearablesSyncQueueHandler);
  });

  it('routes a valid event to the use case', async () => {
    await handler.handleMessage(asMessage(fixture));

    expect(mockUseCase.execute).toHaveBeenCalledTimes(1);
    expect(mockUseCase.execute.mock.calls[0][0].externalUserId).toBe(fixture.external_user_id);
  });

  it('drops a rejected event without calling the use case', async () => {
    await handler.handleMessage(asMessage({ ...fixture, earliest_record_start_at: null }));

    expect(mockUseCase.execute).not.toHaveBeenCalled();
  });

  it('drops a malformed body without throwing', async () => {
    await expect(
      handler.handleMessage({ MessageId: 'm1', Body: 'not json' }),
    ).resolves.toBeUndefined();
    expect(mockUseCase.execute).not.toHaveBeenCalled();
  });

  it('rethrows so SQS retries when the use case fails', async () => {
    mockUseCase.execute.mockRejectedValueOnce(new Error('external api down'));

    await expect(handler.handleMessage(asMessage(fixture))).rejects.toThrow('external api down');
  });
});
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `cd monorepo-backend && npx jest apps/wearables-api/src/services/__tests__/openwearables-sync-queue.handler.spec.ts`
Expected: FAIL — no existe el handler.

- [ ] **Step 3: Escribir el handler**

```typescript
// apps/wearables-api/src/services/openwearables-sync-queue.handler.ts
import { Message } from '@aws-sdk/client-sqs';
import { Injectable, Logger } from '@nestjs/common';
import { SqsMessageHandler } from '@ssut/nestjs-sqs';

import { OpenWearablesSyncEventRaw, parseSyncEvent } from '../dtos/openwearables-sync-event';
import { HandleOpenWearablesSyncUseCase } from '../use-cases/handle-openwearables-sync.use-case';

export const OPEN_WEARABLES_SYNC_QUEUE =
  process.env.OPENWEARABLES_SYNC_QUEUE_NAME || 'open-wearables-sync-events';

@Injectable()
export class OpenWearablesSyncQueueHandler {
  private readonly logger = new Logger(OpenWearablesSyncQueueHandler.name);

  constructor(private readonly handleSync: HandleOpenWearablesSyncUseCase) {}

  @SqsMessageHandler(OPEN_WEARABLES_SYNC_QUEUE, false)
  async handleMessage(message: Message): Promise<void> {
    let raw: OpenWearablesSyncEventRaw;
    try {
      raw = JSON.parse(message.Body || '{}');
    } catch {
      this.logger.error(
        `openwearables.sync_event.discarded reason=malformed_body message=${message.MessageId}`,
      );
      return;
    }

    const parsed = parseSyncEvent(raw);
    if (!parsed.ok) {
      // Descartado, no reintentado: reintentar un evento estructuralmente
      // inválido sólo lo manda a la DLQ más tarde. Avisa la alarma sobre el
      // metric filter de esta línea, que Task 5 define.
      this.logger.warn(
        `openwearables.sync_event.discarded reason=${parsed.reason} ` +
          `provider=${raw.provider} batch=${raw.batch_id}`,
      );
      return;
    }

    // Sin try/catch: un fallo real tiene que propagarse para que SQS reintente
    // y, agotados los reintentos, el mensaje caiga en la DLQ.
    await this.handleSync.execute(parsed.event);
  }
}
```

- [ ] **Step 4: Registrar la cola en el módulo**

En `wearables-api.module.ts`, agregar al array `consumers` del `SqsModule.registerAsync`:

```typescript
{
  name: configService.get<string>('OPENWEARABLES_SYNC_QUEUE_NAME') || 'open-wearables-sync-events',
  queueUrl: configService.get<string>('OPENWEARABLES_SYNC_QUEUE_URL') || '',
  region: configService.get<string>('AWS_REGION') || 'us-east-1',
  // Uno por vez: la cola es FIFO y el snapshot-replace del writer de sueño
  // asume que no hay dos corridas del mismo usuario en paralelo.
  batchSize: 1,
},
```

Y `OpenWearablesSyncQueueHandler` a `providers`.

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd monorepo-backend && npx jest apps/wearables-api`
Expected: PASS, incluidos los tests existentes de Spike.

- [ ] **Step 6: Lint y commit**

```bash
cd monorepo-backend && pnpm run lint:fix && pnpm run format
git add apps/wearables-api
git commit -m "feat(wearables-api): wire the open-wearables sync queue consumer"
```

## Verificación de punta a punta en QA

Después de la Task 10, con todo desplegado en QA:

- [ ] **1.** Prender `SYNC_NOTIFICATIONS_ENABLED` en el worker de Open Wearables y esperar un deploy limpio.
- [ ] **2.** Sincronizar desde un dispositivo real de QA con Apple Health o Health Connect.
- [ ] **3.** Confirmar la emisión: buscar `openwearables.sync_event` y el log del publisher en CloudWatch.
- [ ] **4.** Confirmar el consumo: `openwearables.sleep.window_replaced` en los logs de `wearables-api`.
- [ ] **5.** Confirmar el dato: la home de la app del usuario de prueba muestra lo que sincronizó.
- [ ] **6.** **La noche fragmentada, contra dato real.** Sincronizar una noche completa en varios payloads y confirmar en DocumentDB que hay **un** documento de sueño para esa fecha. Éste es el ítem V1 del spec y es el gate para la primera cohorte.
- [ ] **7.** Confirmar que la DLQ está vacía y que la métrica de eventos descartados es cero.

## Lo que queda para el Plan 2

Enriquecimiento del `metadata` de Whoop, Strava y Oura; deduplicación para Garmin (ítem V4); cohortes de migración; baja diferida de conexiones de Spike; shadow run comparando resúmenes de las dos fuentes; y las alarmas de la sección 11 del spec.
