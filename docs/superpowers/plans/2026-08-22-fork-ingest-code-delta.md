# Delta de código del fork para ingesta en AWS — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar el backend del fork listo para correr en ECS Fargate sin credenciales estáticas de AWS, sin migraciones en el arranque del servicio, sin perder tasks en un scale-in, y con ingesta de batches grandes del SDK vía presigned S3.

**Architecture:** Cuatro cambios acotados sobre el backend existente, más el circuito de ingesta por S3 que el fork ya implementa para el XML de Apple: el cliente pide una URL prefirmada, sube el JSON a S3, el evento del bucket llega por SNS a `/v1/sns/notification`, y el handler despacha por prefijo de clave a la task de procesamiento correspondiente. Ningún componente nuevo fuera del fork.

**Tech Stack:** Python 3.13, FastAPI, Celery, SQLAlchemy 2.0, pytest + testcontainers, uv, ruff + ty, boto3.

**Spec:** `docs/superpowers/specs/2026-08-22-aws-infra-design.md` (secciones 5, 6 y 7)

## Global Constraints

- Python 3.13+. Todos los comandos del backend se corren desde `backend/` con `uv run`.
- Tests: `uv run pytest`. La suite levanta PostgreSQL y Redis con testcontainers, salvo que `TEST_DATABASE_URL` esté definida. Requiere Docker corriendo.
- Lint y tipos antes de cada commit: `uv run pre-commit run --all-files`.
- Commits con conventional commits (`feat`, `fix`, `refactor`, `chore`, `docs`), igual que el historial del repo. Sin trailers autogenerados.
- **No commitear en `main`.** Crear una rama de feature (`feat/s3-sdk-ingest`) antes del primer commit.
- Principio rector del spec: **mínima divergencia con upstream**. Cada cambio de este plan está pensado para ser contribuible upstream. No refactorizar nada que no esté listado.
- El parser de SNS en `app/services/apple/apple_xml/sns_service.py` deriva el `user_id` del **primer segmento** de la clave del objeto y exige **al menos tres segmentos**. Cualquier clave nueva tiene que respetar `{user_id}/{tipo}/{archivo}`.
- Los nombres de bucket, región y topic vienen de `settings` (`AWS_BUCKET_NAME`, `AWS_REGION`, `AWS_SNS_TOPIC_ARN`). No hardcodear ninguno.

## Estructura de archivos

| Archivo | Responsabilidad | Task |
|---|---|---|
| `backend/app/services/apple/apple_xml/aws_service.py` | Fábrica de clientes boto3 (S3, SNS). Se modifica para caer al rol de la task cuando no hay llaves estáticas | 1 |
| `backend/tests/services/test_aws_service.py` | Tests de la fábrica de clientes (nuevo) | 1 |
| `backend/app/integrations/celery/core.py` | Configuración de Celery. Se agrega confirmación tardía de mensajes | 2 |
| `backend/tests/integrations/test_celery_config.py` | Tests de la configuración de fiabilidad (nuevo) | 2 |
| `backend/scripts/start/init.sh` | Migraciones, seeds y data-migrations. Corre como task one-off en ECS (nuevo) | 3 |
| `backend/scripts/start/app.sh` | Delega en `init.sh` y arranca el server | 3 |
| `backend/tests/scripts/test_start_scripts.py` | Verifica el reparto de responsabilidades entre los dos scripts (nuevo) | 3 |
| `backend/app/schemas/providers/sdk_upload.py` | Schema del request de URL prefirmada para el SDK (nuevo) | 4 |
| `backend/app/services/sdk_upload_service.py` | Genera la URL prefirmada y la clave del objeto para batches del SDK (nuevo) | 4 |
| `backend/app/api/routes/v1/sdk_sync.py` | Se agrega el endpoint `POST /sdk/users/{user_id}/sync/s3` | 4 |
| `backend/tests/api/v1/test_sdk_sync_s3.py` | Tests del endpoint y del formato de clave (nuevo) | 4 |
| `backend/app/integrations/celery/tasks/process_s3_sdk_upload_task.py` | Baja el objeto de S3 y delega en el import del SDK (nuevo) | 5 |
| `backend/app/integrations/celery/tasks/__init__.py` | Registro explícito de tasks; se agrega la nueva | 5 |
| `backend/tests/tasks/test_process_s3_sdk_upload_task.py` | Tests de la task (nuevo) | 5 |
| `backend/app/services/apple/apple_xml/sns_service.py` | Se agrega despacho por prefijo de clave | 6 |
| `backend/tests/services/test_sns_dispatch.py` | Tests del despacho por prefijo (nuevo) | 6 |
| `docs/dev-guides/sdk-bulk-upload.mdx` | Documentación del flujo de ingesta por S3 (nuevo) | 7 |
| `docs/docs.json` | Entrada de navegación de la página nueva | 7 |

---

### Task 1: Clientes boto3 con el rol de la task

Hoy `get_s3_client()` llama a `settings.aws_secret_access_key.get_secret_value()` sin condicional. Si no hay llaves estáticas configuradas, `aws_secret_access_key` es `None`, se lanza `AttributeError`, la función la captura y devuelve `None`. Sobre ECS con sólo un rol de task eso significa que el endpoint de presigned devuelve 503 y la task de procesamiento falla. `app/services/raw_payload_storage.py` ya resuelve esto construyendo los kwargs condicionalmente; este task replica ese criterio.

**Files:**
- Modify: `backend/app/services/apple/apple_xml/aws_service.py:14-37`
- Test: `backend/tests/services/test_aws_service.py` (crear)

**Interfaces:**
- Consumes: nada.
- Produces: `get_s3_client() -> Any | None` y `get_sns_client() -> Any | None` con la misma firma que hoy, pero devolviendo un cliente válido cuando no hay credenciales estáticas.

- [ ] **Step 1: Crear la rama de trabajo**

```bash
git switch main && git switch -c feat/s3-sdk-ingest
```

- [ ] **Step 2: Escribir los tests que fallan**

Crear `backend/tests/services/test_aws_service.py`:

```python
"""Tests for the boto3 client factory used by the S3/SNS ingest path."""

from unittest.mock import MagicMock, patch

from pydantic import SecretStr

import app.services.apple.apple_xml.aws_service as aws_service


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_s3_client_omits_credentials_when_unset(mock_boto_client: MagicMock) -> None:
    """Without static keys the client must fall back to the default provider chain
    (the ECS task role), not return None."""
    with (
        patch.object(aws_service.settings, "aws_access_key_id", None),
        patch.object(aws_service.settings, "aws_secret_access_key", None),
    ):
        client = aws_service.get_s3_client()

    assert client is mock_boto_client.return_value
    kwargs = mock_boto_client.call_args.kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_s3_client_passes_static_credentials_when_set(mock_boto_client: MagicMock) -> None:
    """Static keys stay supported for local development and S3-compatible endpoints."""
    with (
        patch.object(aws_service.settings, "aws_access_key_id", "AKIA_TEST"),
        patch.object(aws_service.settings, "aws_secret_access_key", SecretStr("secret-value")),
    ):
        aws_service.get_s3_client()

    kwargs = mock_boto_client.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "AKIA_TEST"
    assert kwargs["aws_secret_access_key"] == "secret-value"


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_sns_client_omits_credentials_when_unset(mock_boto_client: MagicMock) -> None:
    with (
        patch.object(aws_service.settings, "aws_access_key_id", None),
        patch.object(aws_service.settings, "aws_secret_access_key", None),
    ):
        client = aws_service.get_sns_client()

    assert client is mock_boto_client.return_value
    assert "aws_secret_access_key" not in mock_boto_client.call_args.kwargs
```

- [ ] **Step 3: Correr los tests y verificar que fallan**

Run: `cd backend && uv run pytest tests/services/test_aws_service.py -v`
Expected: FAIL. `test_get_s3_client_omits_credentials_when_unset` falla con `assert None is <MagicMock...>` porque hoy la función devuelve `None`.

- [ ] **Step 4: Implementar el cambio**

Reemplazar el cuerpo de `get_s3_client` y `get_sns_client` en `backend/app/services/apple/apple_xml/aws_service.py`:

```python
def _client_kwargs() -> dict[str, Any]:
    """boto3 kwargs: static credentials when configured, otherwise the default
    provider chain — which on ECS resolves to the task role."""
    kwargs: dict[str, Any] = {"region_name": AWS_REGION}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
    return kwargs


def get_s3_client():  # noqa: ANN201
    try:
        return boto3.client("s3", **_client_kwargs())
    except (NoCredentialsError, AttributeError):
        log_structured(logger, "warning", "AWS credentials not configured")
        return None


def get_sns_client():  # noqa: ANN201
    try:
        return boto3.client("sns", **_client_kwargs())
    except (NoCredentialsError, AttributeError):
        log_structured(logger, "warning", "AWS credentials not configured")
        return None
```

Agregar `from typing import Any` al bloque de imports del módulo.

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/services/test_aws_service.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 6: Verificar que no se rompió el path existente**

Run: `cd backend && uv run pytest tests/api/v1/test_import_data.py tests/services/test_raw_payload_storage.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/apple/apple_xml/aws_service.py backend/tests/services/test_aws_service.py
git commit -m "fix: fall back to the default credential chain for S3/SNS clients"
```

---

### Task 2: Confirmación tardía de mensajes en Celery

`create_celery()` no setea `task_acks_late`, así que Celery usa el default `False` y confirma el mensaje al recibirlo. Un contenedor que se apaga por scale-in o deploy en medio de un chunk de backfill pierde ese trabajo sin rastro. El spec (sección 6) exige confirmación tardía con prefetch de 1.

**Files:**
- Modify: `backend/app/integrations/celery/core.py:78-105`
- Test: `backend/tests/integrations/test_celery_config.py` (crear)

**Interfaces:**
- Consumes: nada.
- Produces: `create_celery()` con `conf.task_acks_late is True` y `conf.worker_prefetch_multiplier == 1`.

- [ ] **Step 1: Escribir el test que falla**

Crear `backend/tests/integrations/test_celery_config.py`:

```python
"""Tests for Celery reliability configuration.

A worker that dies mid-task must not lose the message: the broker only gets the
ack once the task finished. See the infra spec, section 6.
"""

from app.integrations.celery.core import create_celery


def test_tasks_are_acknowledged_after_completion() -> None:
    conf = create_celery().conf

    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1


def test_broker_keepalive_options_are_preserved() -> None:
    """Regression guard: the keepalive settings that fixed a stuck consumer upstream
    must survive any change to this config block."""
    options = create_celery().conf.broker_transport_options

    assert options["socket_keepalive"] is True
    assert options["health_check_interval"] == 30
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `cd backend && uv run pytest tests/integrations/test_celery_config.py -v`
Expected: FAIL en `test_tasks_are_acknowledged_after_completion` con `assert False is True`.

- [ ] **Step 3: Implementar el cambio**

En `backend/app/integrations/celery/core.py`, dentro de `celery_app.conf.update(...)`, agregar inmediatamente después del bloque `broker_transport_options`:

```python
        # Ack the message when the task finishes, not when it is received: a worker
        # killed mid-task (scale-in, deploy, OOM) would otherwise drop the work
        # silently. Prefetch 1 keeps a stopping worker from holding a queue of
        # messages it will never process.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
```

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `cd backend && uv run pytest tests/integrations/test_celery_config.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Correr la suite de tasks para descartar regresiones**

Run: `cd backend && uv run pytest tests/tasks -v`
Expected: PASS.

- [ ] **Step 6: Documentar el riesgo residual**

Agregar al final del docstring del módulo `backend/app/integrations/celery/core.py` (o crearlo si no existe) la nota:

```python
"""...

Reliability note: with `task_acks_late` the Redis broker redelivers a message whose
task never acked, bounded by the transport's visibility timeout (1 hour by default).
Two consequences: tasks must stay well under an hour so a slow task is not redelivered
while still running, and every task must be idempotent, because redelivery — like
S3/SNS at-least-once delivery — can run the same payload twice.
"""
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/integrations/celery/core.py backend/tests/integrations/test_celery_config.py
git commit -m "fix: ack Celery tasks after completion so a dying worker does not drop work"
```

---

### Task 3: Extraer las migraciones del arranque del servicio

`scripts/start/app.sh` corre Alembic, seis seeds y dos data-migrations antes de levantar el server. Con más de una task de API eso son N Alembic en paralelo. El spec (D8) requiere que ese bloque pueda correr como task one-off, sin cambiar el comportamiento local.

**Files:**
- Create: `backend/scripts/start/init.sh`
- Modify: `backend/scripts/start/app.sh`
- Test: `backend/tests/scripts/test_start_scripts.py` (crear)

**Interfaces:**
- Consumes: nada.
- Produces: `scripts/start/init.sh`, ejecutable, idempotente, que el pipeline de deploy corre con `RunTask` antes de actualizar los servicios. `scripts/start/app.sh` mantiene su contrato actual: inicializa y arranca el server.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `backend/tests/scripts/test_start_scripts.py`:

```python
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
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd backend && uv run pytest tests/scripts/test_start_scripts.py -v`
Expected: FAIL con `FileNotFoundError` sobre `init.sh`.

- [ ] **Step 3: Crear `init.sh` con el bloque actual, verbatim**

Crear `backend/scripts/start/init.sh`:

```bash
#!/bin/bash
set -e -x

# Database migrations, seeds and one-off data migrations.
#
# Split out of app.sh so an orchestrator can run this exactly once as a standalone
# task before rolling the services, instead of every API replica racing on Alembic.
# Every step here is idempotent and safe to re-run.

# Ensure svix database exists (idempotent)
echo 'Ensuring svix database...'
uv run python scripts/init/create_svix_db.py

# Init database
echo 'Applying migrations...'
uv run alembic upgrade head

# Initialize provider settings
echo 'Initializing provider settings...'
uv run python scripts/init_provider_settings.py

# Initialize device priority table
echo 'Initializing priorities...'
uv run python scripts/init_device_priorities.py

# Seed admin account (uses ADMIN_EMAIL/ADMIN_PASSWORD env vars, or defaults)
echo 'Seeding admin account...'
uv run python scripts/init/seed_admin.py

# Initialize series type definitions
echo 'Initializing series type definitions...'
uv run python scripts/init/seed_series_types.py


# TODO: Remove this after ~2026-09-01 once all deployments have migrated.
# Relabels Oura HRV stored as SDNN (id=3) to RMSSD (id=7); scoped to provider='oura', no-op once corrected.
echo 'Running Oura HRV SDNN->RMSSD relabel...'
uv run python scripts/data_migrations/relabel_oura_hrv_sdnn_to_rmssd.py \
    || echo "Warning: Oura HRV relabel failed — will retry on next startup."


# TODO: Remove this after ~2026-09-01 once all deployments have migrated.
# Labels is_daily_total on archival data_point_series (daily totals → TRUE); idempotent,
# only flips NULL rows, batched. After the first full pass, re-runs are no-ops.
echo 'Running is_daily_total backfill...'
uv run python scripts/data_migrations/backfill_is_daily_total.py \
    || echo "Warning: is_daily_total backfill failed — will retry on next startup."

# Initialize archival settings
echo 'Initializing archival settings...'
uv run python scripts/init/seed_archival_settings.py

# Register webhook event types with Svix (with retry, non-fatal)
echo 'Registering webhook event types...'
for i in 1 2 3; do
    uv run python scripts/init/seed_webhook_event_types.py && break
    echo "Svix not ready yet, retrying in 5s... (attempt ${i}/3)"
    sleep 5
done || echo "Warning: Could not register webhook event types with Svix. Will retry on next startup."
```

Hacerlo ejecutable:

```bash
chmod +x backend/scripts/start/init.sh
```

- [ ] **Step 4: Reducir `app.sh` a delegación más arranque del server**

Reemplazar el contenido completo de `backend/scripts/start/app.sh` por:

```bash
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
```

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/scripts/test_start_scripts.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Verificar el arranque real con docker compose**

```bash
docker compose down -v && docker compose up -d db redis app
docker compose logs -f app | head -60
```

Expected: los logs muestran `Applying migrations...`, los seeds, y después `Starting the FastAPI application...`. Verificar que responde:

```bash
curl -s localhost:8000/ | grep "Server is running"
```

- [ ] **Step 7: Commit**

```bash
git add backend/scripts/start/init.sh backend/scripts/start/app.sh backend/tests/scripts/test_start_scripts.py
git commit -m "refactor: split migrations and seeds out of the app entrypoint"
```

---

### Task 4: Endpoint de URL prefirmada para batches del SDK

Los batches chicos siguen posteando JSON a `/sdk/users/{user_id}/sync`, que valida sincrónicamente y devuelve 202. Los batches grandes — el backfill histórico — piden una URL prefirmada y suben a S3. La clave tiene que ser `{user_id}/sdk/{batch_id}.json` para que el parser de SNS pueda derivar el `user_id`.

**Files:**
- Create: `backend/app/schemas/providers/sdk_upload.py`
- Create: `backend/app/services/sdk_upload_service.py`
- Modify: `backend/app/api/routes/v1/sdk_sync.py`
- Test: `backend/tests/api/v1/test_sdk_sync_s3.py` (crear)

**Interfaces:**
- Consumes: `get_s3_client()` y `AWS_BUCKET_NAME` de `app.services.apple.apple_xml.aws_service` (Task 1). `PresignedURLResponse` de `app.schemas.providers.apple.apple_xml.aws`, con los campos `upload_url`, `form_fields`, `file_key`, `expires_in`, `max_file_size`, `bucket`.
- Produces:
  - `SdkPresignedURLRequest` con `expiration_seconds: int` y `max_file_size: int`.
  - `SDK_KEY_PREFIX = "sdk"` en `app.services.sdk_upload_service` — lo consume Task 6.
  - `sdk_upload_service.create_presigned_url(user_id: str, batch_id: str, request: SdkPresignedURLRequest) -> PresignedURLResponse`.
  - `sdk_upload_service.generate_file_key(user_id: str, batch_id: str) -> str`.
  - Endpoint `POST /api/v1/sdk/users/{user_id}/sync/s3`.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `backend/tests/api/v1/test_sdk_sync_s3.py`:

```python
"""Tests for the presigned S3 upload endpoint used by large SDK batches."""

from logging import getLogger
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.schemas.providers.apple.apple_xml.aws import PresignedURLResponse
from app.services.sdk_upload_service import SdkUploadService
from app.services.sdk_token_service import create_sdk_user_token

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_USER_ID = "99999999-e89b-12d3-a456-426614174000"


def test_object_key_has_three_segments_for_sns_parsing() -> None:
    """The SNS handler derives the user id from the first segment and requires at
    least three segments, so this layout is structural."""
    service = SdkUploadService(getLogger("test"))

    key = service.generate_file_key("user-1", "batch-9")

    assert key == "user-1/sdk/batch-9.json"
    assert len(key.split("/")) >= 3


def test_endpoint_returns_the_upload_form(client: TestClient, api_v1_prefix: str) -> None:
    token = create_sdk_user_token("app_123", USER_ID)
    presigned = PresignedURLResponse(
        upload_url="https://bucket.s3.amazonaws.com/",
        form_fields={"key": f"{USER_ID}/sdk/batch-9.json"},
        file_key=f"{USER_ID}/sdk/batch-9.json",
        expires_in=900,
        max_file_size=200 * 1024 * 1024,
        bucket="test-bucket",
    )

    with patch("app.api.routes.v1.sdk_sync.sdk_upload_service") as service:
        service.create_presigned_url.return_value = presigned
        response = client.post(
            f"{api_v1_prefix}/sdk/users/{USER_ID}/sync/s3",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["file_key"].split("/")[1] == "sdk"
    assert body["form_fields"]["key"].endswith(".json")


def test_endpoint_rejects_a_token_issued_for_another_user(client: TestClient, api_v1_prefix: str) -> None:
    token = create_sdk_user_token("app_123", USER_ID)

    response = client.post(
        f"{api_v1_prefix}/sdk/users/{OTHER_USER_ID}/sync/s3",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 403


def test_endpoint_requires_authentication(client: TestClient, api_v1_prefix: str) -> None:
    response = client.post(f"{api_v1_prefix}/sdk/users/{USER_ID}/sync/s3", json={})

    assert response.status_code in (401, 403)


def test_service_returns_503_when_s3_is_not_configured() -> None:
    from fastapi import HTTPException

    with patch("app.services.sdk_upload_service.get_s3_client", return_value=None):
        service = SdkUploadService(getLogger("test"))

    from app.schemas.providers.sdk_upload import SdkPresignedURLRequest

    try:
        service.create_presigned_url("user-1", "batch-9", SdkPresignedURLRequest())
    except HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("expected HTTPException")


def test_presigned_post_pins_json_content_type() -> None:
    s3_client = MagicMock()
    s3_client.generate_presigned_post.return_value = {"url": "https://s3", "fields": {"key": "k"}}

    with patch("app.services.sdk_upload_service.get_s3_client", return_value=s3_client):
        service = SdkUploadService(getLogger("test"))

    from app.schemas.providers.sdk_upload import SdkPresignedURLRequest

    service.create_presigned_url("user-1", "batch-9", SdkPresignedURLRequest())

    kwargs = s3_client.generate_presigned_post.call_args.kwargs
    assert kwargs["Key"] == "user-1/sdk/batch-9.json"
    assert kwargs["Fields"]["Content-Type"] == "application/json"
    assert ["content-length-range", 1, 200 * 1024 * 1024] in kwargs["Conditions"]
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd backend && uv run pytest tests/api/v1/test_sdk_sync_s3.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.sdk_upload_service'`.

- [ ] **Step 3: Crear el schema del request**

Crear `backend/app/schemas/providers/sdk_upload.py`:

```python
"""Request schema for presigned S3 uploads of large mobile-SDK batches."""

from pydantic import BaseModel, Field

from app.schemas.providers.apple.apple_xml.aws import (
    MAX_EXPIRATION_SECONDS,
    MIN_EXPIRATION_SECONDS,
    MIN_FILE_SIZE,
)

SDK_DEFAULT_EXPIRATION_SECONDS = 900  # 15 minutes
SDK_DEFAULT_MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
SDK_MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GiB


class SdkPresignedURLRequest(BaseModel):
    expiration_seconds: int = Field(
        default=SDK_DEFAULT_EXPIRATION_SECONDS,
        ge=MIN_EXPIRATION_SECONDS,
        le=MAX_EXPIRATION_SECONDS,
        description="URL expiration time in seconds (1 min - 1 hour)",
    )
    max_file_size: int = Field(
        default=SDK_DEFAULT_MAX_FILE_SIZE,
        ge=MIN_FILE_SIZE,
        le=SDK_MAX_FILE_SIZE,
        description="Maximum upload size in bytes (1KB - 1GiB)",
    )
```

- [ ] **Step 4: Crear el servicio**

Crear `backend/app/services/sdk_upload_service.py`:

```python
"""Presigned S3 uploads for large mobile-SDK batches.

Small batches post JSON straight to /sdk/users/{user_id}/sync, which validates
synchronously. Batches above the SDK's size threshold — historical backfill — upload
to S3 instead; the bucket event reaches /sns/notification and dispatches processing.
"""

from logging import Logger, getLogger

from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.schemas.providers.apple.apple_xml.aws import PresignedURLResponse
from app.schemas.providers.sdk_upload import SdkPresignedURLRequest
from app.services.apple.apple_xml.aws_service import AWS_BUCKET_NAME, get_s3_client

# The SNS handler derives the user id from the first segment of the object key and
# requires at least three segments, so this middle segment is structural.
SDK_KEY_PREFIX = "sdk"


class SdkUploadService:
    def __init__(self, log: Logger) -> None:
        self.log = log
        self.s3_client = get_s3_client()

    def generate_file_key(self, user_id: str, batch_id: str) -> str:
        return f"{user_id}/{SDK_KEY_PREFIX}/{batch_id}.json"

    def create_presigned_url(
        self,
        user_id: str,
        batch_id: str,
        request: SdkPresignedURLRequest,
    ) -> PresignedURLResponse:
        if not self.s3_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="S3 client not configured",
            )

        file_key = self.generate_file_key(user_id, batch_id)

        try:
            presigned_post = self.s3_client.generate_presigned_post(
                Bucket=AWS_BUCKET_NAME,
                Key=file_key,
                Fields={"Content-Type": "application/json"},
                Conditions=[
                    ["content-length-range", 1, request.max_file_size],
                    {"Content-Type": "application/json"},
                ],
                ExpiresIn=request.expiration_seconds,
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"S3 error: {error_code}",
            ) from e

        return PresignedURLResponse(
            upload_url=presigned_post["url"],
            form_fields=presigned_post["fields"],
            file_key=file_key,
            expires_in=request.expiration_seconds,
            max_file_size=request.max_file_size,
            bucket=AWS_BUCKET_NAME,
        )


sdk_upload_service = SdkUploadService(getLogger(__name__))
```

- [ ] **Step 5: Agregar el endpoint**

En `backend/app/api/routes/v1/sdk_sync.py`, agregar a los imports:

```python
from app.schemas.providers.apple.apple_xml.aws import PresignedURLResponse
from app.schemas.providers.sdk_upload import SdkPresignedURLRequest
from app.services.sdk_upload_service import sdk_upload_service
```

Y al final del archivo:

```python
@router.post("/sdk/users/{user_id}/sync/s3", status_code=status.HTTP_200_OK)
def create_sdk_sync_upload_url(
    user_id: str,
    body: SdkPresignedURLRequest,
    auth: SDKAuthDep,
) -> PresignedURLResponse:
    """Get a presigned S3 upload for a large SDK batch.

    Batches below the SDK's size threshold keep posting JSON to
    `/sdk/users/{user_id}/sync`, which validates the request synchronously. Larger
    batches — historical backfill — upload here and are processed from the bucket
    event, keeping multi-megabyte payloads out of the request body and the broker.

    Args:
        user_id: SDK user identifier
        body: expiration and size limits for the upload
        auth: SDK authentication (Bearer token or API key)

    Returns:
        PresignedURLResponse with the form to POST the batch to S3.

    Raises:
        HTTPException: 403 if the token does not match user_id, 503 if S3 is not configured.
    """
    if auth.auth_type == "sdk_token" and (not auth.user_id or str(auth.user_id) != user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token does not match user_id",
        )

    batch_id = str(uuid.uuid4())

    return sdk_upload_service.create_presigned_url(user_id, batch_id, body)
```

- [ ] **Step 6: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/api/v1/test_sdk_sync_s3.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 7: Verificar que el endpoint viejo sigue intacto**

Run: `cd backend && uv run pytest tests/api/v1/test_sdk_sync_auth.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/providers/sdk_upload.py backend/app/services/sdk_upload_service.py \
        backend/app/api/routes/v1/sdk_sync.py backend/tests/api/v1/test_sdk_sync_s3.py
git commit -m "feat: add presigned S3 upload endpoint for large SDK batches"
```

---

### Task 5: Task que procesa un batch del SDK desde S3

Espejo de `process_aws_upload` para payloads JSON: baja el objeto y lo entrega al mismo camino de import que usa el endpoint directo, así no hay dos implementaciones del import.

**Files:**
- Create: `backend/app/integrations/celery/tasks/process_s3_sdk_upload_task.py`
- Test: `backend/tests/tasks/test_process_s3_sdk_upload_task.py` (crear)

**Interfaces:**
- Consumes: `get_s3_client()` de `app.services.apple.apple_xml.aws_service` (Task 1). `process_sdk_upload(content: str, content_type: str, user_id: str, provider: str, batch_id: str | None) -> dict[str, Any]` de `app.integrations.celery.tasks.process_sdk_upload_task`.
- Produces: `process_s3_sdk_upload(bucket_name: str, object_key: str, user_id: str) -> dict[str, Any]`, task de Celery — la consume Task 6.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `backend/tests/tasks/test_process_s3_sdk_upload_task.py`:

```python
"""Tests for the process_s3_sdk_upload Celery task."""

import io
import json
from unittest.mock import MagicMock, patch

from app.integrations.celery.tasks.process_s3_sdk_upload_task import process_s3_sdk_upload

MODULE = "app.integrations.celery.tasks.process_s3_sdk_upload_task"


def _s3_client_returning(body: str) -> MagicMock:
    client = MagicMock()
    client.get_object.return_value = {"Body": io.BytesIO(body.encode("utf-8"))}
    return client


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_downloads_the_object_and_delegates_to_the_sdk_import(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    body = json.dumps({"provider": "apple", "data": {"records": []}})
    mock_get_client.return_value = _s3_client_returning(body)
    mock_process.return_value = {"status": "success"}

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    mock_get_client.return_value.get_object.assert_called_once_with(
        Bucket="ingest-bucket", Key="user-1/sdk/batch-9.json"
    )
    mock_process.assert_called_once_with(
        content=body,
        content_type="application/json",
        user_id="user-1",
        provider="apple",
        batch_id="batch-9",
    )
    assert result == {"status": "success"}


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_unsupported_provider_is_not_dispatched(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    mock_get_client.return_value = _s3_client_returning(json.dumps({"provider": "fitbit"}))

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_malformed_json_is_not_dispatched(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    mock_get_client.return_value = _s3_client_returning("not json at all")

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()


@patch(f"{MODULE}.get_s3_client", return_value=None)
def test_raises_when_s3_is_not_configured(_mock_get_client: MagicMock) -> None:
    try:
        process_s3_sdk_upload(
            bucket_name="ingest-bucket",
            object_key="user-1/sdk/batch-9.json",
            user_id="user-1",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError when the S3 client is unavailable")
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd backend && uv run pytest tests/tasks/test_process_s3_sdk_upload_task.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar la task**

Crear `backend/app/integrations/celery/tasks/process_s3_sdk_upload_task.py`:

```python
"""Process a mobile-SDK batch uploaded to S3.

Mirror of process_aws_upload for JSON batches: downloads the object and hands the
payload to the same import path the direct-POST endpoint uses, so there is exactly
one implementation of the SDK import.
"""

import json
from logging import getLogger
from typing import Any

from celery import shared_task

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

SUPPORTED_PROVIDERS = ("apple", "samsung", "google")


@shared_task
def process_s3_sdk_upload(bucket_name: str, object_key: str, user_id: str) -> dict[str, Any]:
    """Download an SDK batch from S3 and import it.

    Args:
        bucket_name: S3 bucket name
        object_key: S3 object key, shaped `{user_id}/sdk/{batch_id}.json`
        user_id: user the batch belongs to
    """
    s3_client = get_s3_client()
    if not s3_client:
        err = RuntimeError("S3 client not configured — cannot process SDK upload")
        log_and_capture_error(
            err,
            logger,
            "S3 client unavailable in process_s3_sdk_upload task",
            extra={"bucket_name": bucket_name, "object_key": object_key, "user_id": user_id},
        )
        raise err

    content = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read().decode("utf-8")

    # batch_id travels in the key so the whole pipeline shares one correlation id.
    batch_id = object_key.rsplit("/", 1)[-1].removesuffix(".json")

    try:
        provider = str(json.loads(content).get("provider") or "").lower()
    except json.JSONDecodeError:
        provider = ""

    if provider not in SUPPORTED_PROVIDERS:
        log_structured(
            logger,
            "warning",
            f"Unsupported or missing provider in S3 SDK batch: {provider!r}",
            action="s3_sdk_batch_rejected",
            batch_id=batch_id,
            user_id=user_id,
            object_key=object_key,
        )
        return {
            "status": "error",
            "reason": f"unsupported_provider: {provider}",
            "batch_id": batch_id,
            "object_key": object_key,
        }

    log_structured(
        logger,
        "info",
        f"{provider.capitalize()} S3 batch received",
        action=f"{provider}_s3_batch_received",
        batch_id=batch_id,
        user_id=user_id,
        provider=provider,
        object_key=object_key,
    )

    return process_sdk_upload(
        content=content,
        content_type="application/json",
        user_id=user_id,
        provider=provider,
        batch_id=batch_id,
    )
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/tasks/test_process_s3_sdk_upload_task.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Registrar la task en el paquete de tasks**

Las tasks se registran por import explícito en `backend/app/integrations/celery/tasks/__init__.py`, no por autodiscovery. Agregar la línea de import inmediatamente después de `from .process_aws_upload_task import process_aws_upload`:

```python
from .process_s3_sdk_upload_task import process_s3_sdk_upload
```

Y agregar la entrada al `__all__`, junto a las otras tasks de procesamiento (`"process_sdk_upload"`, `"process_aws_upload"`, `"process_xml_upload"`):

```python
    "process_s3_sdk_upload",
```

- [ ] **Step 6: Verificar que Celery la reconoce**

Run: `cd backend && uv run python -c "from app.main import celery_app; print('app.integrations.celery.tasks.process_s3_sdk_upload_task.process_s3_sdk_upload' in celery_app.tasks)"`
Expected: `True`

- [ ] **Step 7: Commit**

```bash
git add backend/app/integrations/celery/tasks/process_s3_sdk_upload_task.py \
        backend/app/integrations/celery/tasks/__init__.py \
        backend/tests/tasks/test_process_s3_sdk_upload_task.py
git commit -m "feat: add task to process SDK batches uploaded to S3"
```

---

### Task 6: Despacho por prefijo de clave en el handler de SNS

`_process_s3_notification` despacha todo a `process_aws_upload`, que importa XML. Con dos tipos de objeto en el bucket hay que rutear por el segmento de tipo de la clave.

**Files:**
- Modify: `backend/app/services/apple/apple_xml/sns_service.py:142-189`
- Test: `backend/tests/services/test_sns_dispatch.py` (crear)

**Interfaces:**
- Consumes: `SDK_KEY_PREFIX` de `app.services.sdk_upload_service` (Task 4). `process_s3_sdk_upload` de Task 5.
- Produces: el despacho completo del flujo. No expone interfaces nuevas.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `backend/tests/services/test_sns_dispatch.py`:

```python
"""Tests for S3-event dispatch by object-key prefix.

One bucket carries two kinds of object — Apple Health XML exports under `raw/` and
mobile-SDK JSON batches under `sdk/` — and each goes to a different import task.
"""

import json
from unittest.mock import MagicMock, patch

from app.schemas.providers.apple.apple_xml.aws import SNSNotification
from app.services.apple.apple_xml.sns_service import sns_service

MODULE = "app.services.apple.apple_xml.sns_service"


def _notification_for(object_key: str) -> SNSNotification:
    message = json.dumps(
        {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "s3": {
                        "bucket": {"name": "ingest-bucket"},
                        "object": {"key": object_key},
                    },
                }
            ]
        }
    )
    return SNSNotification(
        Type="Notification",
        MessageId="msg-1",
        TopicArn="arn:aws:sns:us-east-1:123456789012:ingest",
        Message=message,
        Timestamp="2026-08-22T00:00:00.000Z",
        Signature="sig",
        SignatureVersion="1",
        SigningCertURL="https://sns.us-east-1.amazonaws.com/cert.pem",
    )


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_sdk_prefix_goes_to_the_sdk_task(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("user-1/sdk/batch-9.json"))

    mock_sdk_task.delay.assert_called_once_with(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )
    mock_xml_task.delay.assert_not_called()
    assert result.status_code == 202


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_raw_prefix_still_goes_to_the_xml_task(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("user-1/raw/export.xml"))

    mock_xml_task.delay.assert_called_once_with(
        bucket_name="ingest-bucket",
        object_key="user-1/raw/export.xml",
        user_id="user-1",
    )
    mock_sdk_task.delay.assert_not_called()
    assert result.status_code == 202


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_key_without_enough_segments_dispatches_nothing(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("orphan.json"))

    mock_xml_task.delay.assert_not_called()
    mock_sdk_task.delay.assert_not_called()
    assert "0 tasks dispatched" in result.response
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `cd backend && uv run pytest tests/services/test_sns_dispatch.py -v`
Expected: FAIL. `test_sdk_prefix_goes_to_the_sdk_task` falla porque hoy se despacha `process_aws_upload` para cualquier clave, y `patch` de `process_s3_sdk_upload` falla con `AttributeError` porque el módulo no lo importa todavía.

- [ ] **Step 3: Implementar el despacho**

En `backend/app/services/apple/apple_xml/sns_service.py`, agregar a los imports:

```python
from app.integrations.celery.tasks.process_s3_sdk_upload_task import process_s3_sdk_upload
from app.services.sdk_upload_service import SDK_KEY_PREFIX
```

Y reemplazar el bloque de despacho dentro del `for record in records:` — el que hoy llama a `process_aws_upload.delay(...)` — por:

```python
            # One bucket, two kinds of object: `{user}/sdk/*.json` are mobile-SDK
            # batches, `{user}/raw/*.xml` are Apple Health exports.
            if object_key_parts[1] == SDK_KEY_PREFIX:
                process_s3_sdk_upload.delay(
                    bucket_name=bucket_name,
                    object_key=object_key,
                    user_id=user_id,
                )
            else:
                process_aws_upload.delay(
                    bucket_name=bucket_name,
                    object_key=object_key,
                    user_id=user_id,
                )
            dispatched += 1
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `cd backend && uv run pytest tests/services/test_sns_dispatch.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Correr la suite completa**

Run: `cd backend && uv run pytest`
Expected: PASS. Si algo del path de XML se rompió, arreglarlo antes de commitear.

- [ ] **Step 6: Lint y tipos**

Run: `cd backend && uv run pre-commit run --all-files`
Expected: sin errores.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/apple/apple_xml/sns_service.py backend/tests/services/test_sns_dispatch.py
git commit -m "feat: dispatch S3 events by key prefix to the matching import task"
```

---

### Task 7: Documentar el flujo de ingesta por S3

El equipo de mobile necesita saber cuándo usar cada camino y qué formato de clave espera el backend.

**Files:**
- Create: `docs/dev-guides/sdk-bulk-upload.mdx`
- Modify: `docs/docs.json`

**Interfaces:**
- Consumes: el endpoint y el formato de clave de Task 4.
- Produces: nada de código.

- [ ] **Step 1: Escribir la página**

Crear `docs/dev-guides/sdk-bulk-upload.mdx`:

```mdx
---
title: "Bulk SDK uploads via S3"
description: "Upload large mobile-SDK batches to S3 with a presigned form instead of posting them in the request body."
---

## When to use which path

The mobile SDK has two ways to deliver a batch:

| Path | Use it for | Feedback |
|---|---|---|
| `POST /api/v1/sdk/users/{user_id}/sync` | Incremental syncs under ~1 MB | Synchronous `400`/`403` on a bad request, `202` once queued |
| `POST /api/v1/sdk/users/{user_id}/sync/s3` | Historical backfill and any batch above the threshold | Validation happens asynchronously, after the upload |

Large batches take the S3 path so a multi-megabyte payload never travels through the
request body or the task broker.

## Flow

1. The SDK calls `POST /api/v1/sdk/users/{user_id}/sync/s3` with its SDK token.
   The response contains `upload_url`, `form_fields`, `file_key` and `expires_in`.
2. The SDK posts the batch to `upload_url` as `multipart/form-data`, sending every
   entry of `form_fields` first and the JSON body last, with
   `Content-Type: application/json`.
3. The bucket notification reaches `POST /api/v1/sns/notification`, which verifies
   the SNS signature and dispatches the import.

The presigned form expires in 15 minutes by default and caps the upload at 200 MB.
Both are adjustable per request via `expiration_seconds` and `max_file_size`.

## Object key layout

Keys are `{user_id}/sdk/{batch_id}.json`. The middle segment is what routes the
object to the SDK import rather than the Apple Health XML import, and the first
segment is how the notification handler recovers the user. Do not flatten the key.

## Retries

Delivery is at-least-once: a batch can be processed twice, so uploading the same
payload again is safe and does not duplicate data points.
```

- [ ] **Step 2: Agregar la página a la navegación**

En `docs/docs.json`, dentro del grupo de `dev-guides` de la pestaña de Guides, agregar la entrada al array `pages`:

```json
"dev-guides/sdk-bulk-upload"
```

- [ ] **Step 3: Verificar que el JSON sigue siendo válido**

Run: `python3 -c "import json; json.load(open('docs/docs.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add docs/dev-guides/sdk-bulk-upload.mdx docs/docs.json
git commit -m "docs: document bulk SDK uploads via presigned S3"
```

---

## Fuera del alcance de este plan

Los siguientes puntos del spec se implementan en planes separados:

| Plan | Contenido |
|---|---|
| 2 | Fundaciones de infra en qa: módulos `network` y `data` (VPC, endpoints, Aurora, ElastiCache) |
| 3 | Plataforma, servicios y pipeline en qa: cluster, ALB, WAF, cinco servicios ECS, workflow de deploy y rol OIDC |
| 4 | `sdk-ingest` y observabilidad en qa: bucket, SNS, DLQ, alarmas, canary. Después, replicar 2–4 en prod |

Dependencia entre planes: este plan no depende de ninguno de los otros y puede ejecutarse en paralelo. El plan 4 depende de que este esté mergeado, porque el bucket y la suscripción SNS apuntan a los endpoints que se crean acá.

## Ítems de verificación abiertos que tocan este plan

- **V2 (bloqueante para el backfill):** confirmar que el upsert de datos es idempotente ante entrega duplicada de SNS o redelivery del broker. La sección de retries de la documentación de Task 7 lo afirma; si no es cierto, hay que hacerlo cierto antes de abrir el primer lote de migración y actualizar el spec.
