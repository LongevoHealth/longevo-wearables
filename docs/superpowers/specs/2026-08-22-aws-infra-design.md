# Diseño de infraestructura AWS para longevo-wearables

**Fecha:** 2026-08-22
**Estado:** aprobado para pasar a plan de implementación
**Alcance:** fase 1 — reemplazo de Spike por el fork de Open Wearables, desplegado en AWS con Terraform

---

## 1. Contexto y objetivo

Longevo consume hoy datos de wearables a través de Spike, con un volumen aproximado de **400.000 eventos por día**. El objetivo es reemplazarlo por este fork de Open Wearables, corriendo en infraestructura propia de AWS, gestionada con Terraform en el repositorio `longevoIac`.

El principio rector es **desviarse lo mínimo posible del upstream**. Cada pieza de infraestructura que se reemplaza dentro del código es un delta a mantener en cada sincronización con Open Wearables. Este diseño adapta la topología del `docker-compose.yml` a servicios gestionados de AWS, y limita el cambio de código a cuatro piezas acotadas y con forma de upstream (sección 7).

## 2. Alcance de fase 1

**Dentro:**

- Ambientes `qa` (us-west-2) y `prod` (us-east-1).
- Servicios: API FastAPI, `celery-worker`, `celery-beat`, worker dedicado de backfill, frontend (SSR).
- Aurora PostgreSQL Serverless v2, ElastiCache, ALB público con WAF, ECR, Secrets Manager, S3.
- Ingesta exclusivamente por SDK: **Apple Health y Health Connect (Samsung/Google)**.
- Migración desde Spike con re-vinculación de usuarios y backfill histórico.

**Fuera:**

- `svix-server` y webhooks salientes. `OUTGOING_WEBHOOKS_ENABLED` es `false` por default y la app tolera la ausencia de Svix (`backend/config/.env.example`). Esto elimina el servicio ECS de Svix, la base `svix` y el problema de `create-svix-db.sql`.
- Flower. La inspección de Celery se hace con `aws ecs execute-command` + `celery inspect`.
- Providers OAuth (Garmin, Oura, Whoop, Fitbit, Polar, Strava, Suunto, Ultrahuman). Se incorporan en fase 2, cuando haya cuentas de developer propias de Longevo.
- Ambiente `stg`.
- NAT Gateway (no hay tráfico de salida en fase 1; ver sección 4).

## 3. Decisiones y justificación

| # | Decisión | Justificación |
|---|---|---|
| D1 | Ingesta por el path nativo del fork (ALB → API → Celery), con presigned S3 para batches grandes | A 400k eventos/día el pico es ~70 rps, absorbible con 2 tasks. El endpoint del SDK ya es un enqueue barato que devuelve 202. Un buffer Lambda→SQS agregaría dos componentes y un consumidor que el fork no tiene |
| D2 | No se implementa consumidor de SQS dentro de FastAPI | Nada en el fork consume SQS (`boto3` se usa sólo para S3). Escribirlo es divergencia permanente contra upstream |
| D3 | SQS se usa únicamente como DLQ de la suscripción SNS | Es el único punto del flujo donde un mensaje se perdería en silencio. Cuesta centavos y no requiere código |
| D4 | Frontend como servicio ECS Fargate, no S3 + CloudFront | Es TanStack Start + Nitro: un server Node, no un SPA estático (`frontend/Dockerfile` corre `.output`) |
| D5 | Aurora PostgreSQL Serverless v2 | El perfil de carga es un pico de semanas por el backfill y después régimen bajo. Provisioned obligaría a pagar el pico todo el año |
| D6 | Aurora 16 o 17, no 18 | Nada en el fork requiere PostgreSQL 18: sólo se usa `jsonb_path_ops` |
| D7 | Sin NAT Gateway en fase 1 | Sin providers OAuth no hay tráfico de salida. VPC endpoints cubren ECR, S3, Logs, Secrets y SSM. Cuesta ~$35/mes más que un NAT, pero cierra el egress por diseño |
| D8 | Migraciones fuera del arranque del contenedor de la API | `scripts/start/app.sh` corre Alembic y seeds antes del server; con más de una task eso son N Alembic en paralelo |
| D9 | Cola `sdk_sync` atendida por un servicio de worker separado | Un backfill masivo no debe ahogar el sync incremental ni el cálculo de scores. Es sólo otra task definition con distinto `-Q`: cero código |
| D10 | Proyecto nuevo en longevoIac, apply automático en todos los ambientes | Estado y ciclo de vida propios, respetando la convención vigente del repo |

## 4. Red y seguridad

Una VPC propia por ambiente, sin peering ni rutas hacia las VPCs de Longevo. El consumo desde el resto de Longevo es por **ALB público con WAF y API key**.

| Capa | Máscara por AZ | Contenido |
|---|---|---|
| Pública | `/24` | Solo el ALB |
| Privada | `/20` | Tasks de ECS |
| Aislada | `/24` | Aurora y ElastiCache, sin ruta de salida |

- `qa`: `10.60.0.0/16`, 2 AZs. `prod`: `10.61.0.0/16`, 3 AZs.

**V3 resuelto.** Los tres ambientes de Longevo viven en la misma cuenta (`577082859150`), y el inventario de CIDRs en uso es:

| Ambiente | Región | VPC | CIDR | Pool de clientes VPN |
|---|---|---|---|---|
| prod | us-east-1 | `vpc-7a33ad07` — **default** | `172.31.0.0/16` | `10.200.0.0/22` |
| qa | us-west-2 | `vpc-0dc8f675` — **default** | `172.31.0.0/16` | `10.100.0.0/22` |
| staging | us-east-2 | `longevo-staging-vpc` | `10.10.0.0/16` | `10.150.0.0/22` |

`10.60.0.0/16` y `10.61.0.0/16` no colisionan con ninguno de esos rangos, así que quedan confirmados. Dos observaciones que salen del inventario y que conviene tener a la vista:

- **prod y qa corren en la VPC default**, las dos con el mismo `172.31.0.0/16`. Entre sí nunca van a poder peerearse, y cualquier peering futuro contra ellas hereda esa ambigüedad. Un argumento más para que wearables tenga su propia VPC con un rango elegido.
- **El Client VPN existente está asociado a la VPC default de cada región.** Si el equipo va a necesitar acceso privado a Aurora o a las tasks de wearables, no alcanza con estar conectado a la VPN actual: hay que crear un endpoint propio en la VPC de wearables, o peerear y agregar reglas de autorización. Está presupuestado como acceso por SSM Session Manager, que evita las dos cosas.

**Lo que el inventario abre y este diseño no resuelve: el aislamiento es de red, no de cuenta.** La VPC de wearables queda independiente, pero comparte cuenta con todo el resto de Longevo, así que comparte el plano de IAM, los límites de servicio, CloudTrail y el radio de explosión de una credencial comprometida. Para HIPAA e ISO 27001 una cuenta separada es un límite materialmente más fuerte que una VPC separada, y era el espíritu del pedido original. No lo decido acá porque implica Organizations, facturación y el pipeline de OIDC; queda como decisión explícita (ítem V9).

**VPC endpoints** (sin NAT): interface para `ecr.api`, `ecr.dkr`, `logs`, `secretsmanager`, `ssmmessages`; gateway para S3. Los endpoints de interface se pagan por AZ, así que van en las 3 AZs de `prod` y en una sola AZ en `qa`.

**WAF regional sobre el ALB:**

- Managed rules: Core rule set, Known Bad Inputs, IP Reputation.
- **Sin** la lista de Anonymous IP: los usuarios entran por NAT de carrier y VPNs móviles, lo que produce falsos positivos.
- `SizeRestrictions_BODY` **en override**. Bloquea bodies > 8 KB y los batches del SDK son mayores. Sin esta excepción la ingesta no funciona.
- Regla rate-based por IP con umbral alto y scope-down al path de ingesta, **excluyendo** `/v1/sns/notification` (SNS entrega en ráfagas y ya trae firma verificada).
- Logging del WAF a CloudWatch con el header `Authorization` redactado.

**ALB:** solo HTTPS con política TLS moderna, certificado ACM, redirección 80→443, access logs a S3 con CMK, `deletion_protection` en prod. Routing por host: `api.*` al target group de la API (8000), `app.*` al del frontend (3000).

**Security groups en cadena**, sin CIDRs sueltos: el ALB acepta 443 de internet; API y frontend aceptan su puerto sólo del SG del ALB; worker y beat no aceptan ingress; Aurora acepta 5432 sólo de los SGs de api/worker/beat/migración; ElastiCache igual con su puerto.

**Cifrado y evidencia:** CMK de KMS por ambiente con rotación anual. Aurora cifrada con esa CMK y `rds.force_ssl=1`. ElastiCache con cifrado at-rest, in-transit y AUTH token en Secrets Manager (el fork ya soporta `REDIS_SSL`, `REDIS_USERNAME`, `REDIS_PASSWORD`). Buckets con SSE-KMS, block public access, versionado y política que exige `aws:SecureTransport`. VPC Flow Logs, logs de WAF y de ALB persistidos. `LOG_ERROR_RESPONSE_BODY=false` en prod para que no entre PHI a los logs.

**Rotación de secretos.** ECS inyecta secretos al arrancar la task: si rota la contraseña de la base, las tasks vivas quedan con la credencial vieja. Autenticación IAM de RDS no es opción porque el fork usa `db_password` estático (sería divergencia). Solución adoptada:

- Master de Aurora gestionado por RDS con rotación automática — la app no lo usa.
- Secreto del usuario de aplicación con rotación mensual, que dispara un `force-new-deployment` automatizado de los servicios.
- `MASTER_KEY` (clave Fernet) **sin rotación**, porque descifra los provider settings almacenados, con procedimiento de recuperación documentado.

## 5. Servicios ECS y pipeline de deploy

Un cluster Fargate por ambiente. Cinco servicios, dos imágenes: API, worker, worker de backfill y beat comparten la imagen del backend y se diferencian por el `command`, igual que en el compose.

| Servicio | Command | Tasks | CPU / Mem | Autoscaling |
|---|---|---|---|---|
| `api` | server FastAPI | 2 | 0.5 vCPU / 1 GB | 2→8 por `RequestCountPerTarget` + CPU |
| `celery-worker` | `scripts/start/worker.sh` (`-Q default,webhook_sync,garmin_sync`) | 2 | 1 vCPU / 2 GB | 2→10 por CPU |
| `celery-worker-bulk` | worker con `-Q sdk_sync` | 1 | 2 vCPU / 4 GB | 1→10 por CPU |
| `celery-beat` | `scripts/start/beat.sh` | 1 | 0.25 / 0.5 GB | ninguno |
| `frontend` | server Nitro | 2 | 0.25 / 0.5 GB | 2→4 por CPU |

- El worker de backfill descarga el archivo a `/tmp` y lo parsea en memoria (`backend/app/integrations/celery/tasks/process_aws_upload_task.py`): necesita ephemeral storage por encima de los 20 GB por defecto durante la ventana de migración.
- El worker es IO-bound (`--pool=threads`), así que la CPU es una señal de escalado mediocre. Fase 1 escala por CPU; la profundidad de cola como métrica de escalado queda para fase 2.
- **El worker de backfill se define por override de command en la task definition, y ahí van dos cosas que no pueden faltar:** `--concurrency=1`, porque el default es `os.cpu_count()` y en Fargate eso puede reportar los cores del host y no las vCPU de la task — dos batches concurrentes contra el techo de memoria es precisamente el escenario de OOM; y `--pool=prefork` si se quiere que los límites de tiempo por task se apliquen, porque **el thread pool de Celery descarta `soft_time_limit` y `task_time_limit`**. Con `--pool=threads` esos límites quedan declarados pero inertes.
- `task_acks_late=True` y `worker_prefetch_multiplier=1` en la configuración de Celery, más `stopTimeout=120` en el contenedor, para que un scale-in o un deploy no pierda la task en curso (ver sección 6).
- `celery-beat` se despliega con `deployment_minimum_healthy_percent=0` y `maximum_percent=100`, para que nunca haya dos schedulers vivos simultáneamente.

**Health checks.** `GET /` de la API devuelve `{"message": "Server is running!"}` sin tocar la base (`backend/app/main.py:77`). Sirve como liveness del target group, pero **es sólo liveness**: si Aurora se cae la API sigue devolviendo 200. La salud de la base va por alarmas, no por el balanceador. El frontend chequea `/` en el 3000.

**Cambio de código requerido (D8):** extraer el bloque de inicialización de `backend/scripts/start/app.sh` a un `scripts/start/init.sh`, y que `app.sh` lo invoque antes de arrancar el server. El comportamiento local con `docker compose` queda idéntico; en ECS la task de migración corre `init.sh` y el servicio de la API arranca sólo el server. Es un refactor de dos archivos, sin cambio de comportamiento, contribuible upstream.

**Frontera Terraform / pipeline**, siguiendo la convención de `src/modules/ecs/base`:

- **Terraform (longevoIac):** cluster, ECR con tags inmutables y scan on push, log groups, roles de task y ejecución, security groups, ALB con target groups y reglas, servicios ECS con `ignore_changes` en `task_definition` y `desired_count`, políticas de autoscaling, Aurora, ElastiCache, contenedores de secretos, buckets, SNS, DLQ y WAF.
- **Repo del fork (longevo-wearables):** build de las dos imágenes, push a ECR con tag inmutable igual al SHA de git, registro de las revisiones reales de task definition, y orden del rollout.

**Orden del rollout:**

1. Build y push de ambas imágenes.
2. Registrar las nuevas revisiones de task definition.
3. `RunTask` de la migración y esperar exit code 0. Si falla, abortar **sin tocar ningún servicio**.
4. `update-service` de api, frontend y los dos workers, con deployment circuit breaker y rollback automático.
5. `update-service` de beat con `min 0 / max 100`.
6. Esperar estabilidad de los cinco servicios.

**Regla de disciplina que se desprende:** las migraciones de Alembic deben ser **aditivas**. Durante el paso 4 conviven tasks viejas y nuevas contra el mismo esquema; los `DROP COLUMN` y renombres van en un deploy posterior al que introduce la lectura nueva.

**Accesos:** rol OIDC nuevo para `longevo-wearables`, acotado a push de ECR, `RegisterTaskDefinition`, `UpdateService`, `RunTask` y `PassRole` sobre los dos roles de task. El módulo `github-oidc-trust` ya existe en longevoIac. `enable_execute_command` habilitado en los servicios, con logging de sesión para auditoría.

## 6. Datos

### Aurora PostgreSQL Serverless v2

| Parámetro | prod | qa |
|---|---|---|
| Topología | writer + 1 reader en otra AZ, failover automático | single-AZ, sin reader |
| Rango de ACU | 2–16 (techo temporal 32 durante el backfill) | 0–4 con auto-pause |
| PITR | 35 días | 7 días |
| `deletion_protection` | sí | sí |
| Backups cross-region | sí | no |

- **I/O-Optimized:** arrancar en Standard, medir el costo de I/O el primer mes y cambiar si supera ~25% del total. Aurora permite el switch una vez por mes.
- **Parameter group:** `rds.force_ssl=1`, `log_min_duration_statement=1000`, `log_statement=ddl`, pgaudit limitado a DDL y cambios de roles. Auditar DML sobre tablas de datos de salud generaría un volumen impagable sin valor forense.
- **Tres usuarios, no uno:** master gestionado por RDS (rotación automática, no usado por la app); `app` para runtime sin `CREATEDB` ni `SUPERUSER`; `migrator` con DDL, usado sólo por la task de migración. Separar migrador de runtime es control de cambios y evita el problema del privilegio de crear bases.
- `scripts/init/create_svix_db.py` ya captura la excepción y continúa, con el comentario explícito *"managed Postgres without CREATEDB"*. No requiere trabajo.

**Crecimiento de datos.** La variable no determinada es cuántas filas de `data_point_series` genera un día de un usuario típico. Palancas disponibles en el fork:

- `DEFAULT_DATA_GRANULARITY` (`raw` / `hourly` / `daily`).
- `INGEST_WORKOUT_SAMPLES=false` (default).
- Archival diario a las 03:00 UTC: agrega las filas más viejas que `archive_after_days` a un archivo diario y borra el archivo pasado `delete_after_days`, ambos configurables por admin.

Recomendación: si el producto consume summaries y scores, arrancar en `hourly` baja el volumen un orden de magnitud sin pérdida funcional. Pasar a `raw` es una decisión de producto con precio medible en la línea de Aurora.

### ElastiCache

Replication group con primary + réplica, Multi-AZ con failover automático, cifrado at-rest e in-transit, AUTH token en Secrets Manager.

**Motor: Valkey. V1 resuelto** — ElastiCache no ofrece Redis OSS 8. Las versiones soportadas de Redis OSS cortan en **7.1**; Valkey llega a **9.1** (docs de ElastiCache, *Engine versions and upgrading*). El `redis:8` del compose no tiene equivalente, así que la elección real es Valkey 9.x o quedarse en Redis OSS 7.1. Valkey es wire-compatible con lo que usan Celery y redis-py.

Y hay un motivo de diseño, no sólo de versión, para ir a Valkey 9.x: **introduce durability por log transaccional Multi-AZ**, con escritura síncrona (cero pérdida) o asíncrona (hasta 10 s en riesgo). Un broker de Celery durable cambia la ecuación que motivaba discutir SQS: el argumento de "una cola gestionada con durabilidad real" se cubre sin migrar el sistema de tareas ni divergir del upstream. Queda por verificar en la implementación que la durability sea transparente al protocolo que usa Celery, y cuánto cuesta frente a un replication group sin ella — pero si lo es, refuerza D1 y D2 en lugar de debilitarlos.

Dos hallazgos del código que determinan la configuración:

**`maxmemory-policy` en `noeviction`.** El comentario en `backend/app/integrations/celery/core.py:83` documenta que upstream ya sufrió que la cola creciera hasta que *"Redis hit maxmemory"*. Redis es además result backend con `result_expires` de 3 días, lo que a 400k tasks/día son muchas claves que nadie lee. El parameter group por defecto de ElastiCache usa `volatile-lru`, que desaloja primero justamente esas claves con TTL, en silencio. `noeviction` hace que la presión de memoria falle de forma visible. Complementar con alarma de memoria y bajando `result_expires` o ignorando el resultado en las tasks fire-and-forget.

**`task_acks_late` no está seteado**, así que Celery usa el default `False` y confirma el mensaje al recibirlo, no al terminarlo. Un contenedor que se apaga por scale-in, deploy u OOM en medio de un chunk de backfill **pierde ese trabajo sin rastro**. Corrección: `task_acks_late=True`, `worker_prefetch_multiplier=1` y `stopTimeout=120`.

## 7. Ingesta

El fork ya implementa el circuito completo de ingesta por S3 para el XML de Apple:

`POST /v1/users/{user_id}/import/apple/xml/s3` devuelve un presigned POST con condiciones de `content-length-range` y `Content-Type`, y clave `{user_id}/raw/{filename}`. El cliente sube directo a S3. El evento del bucket va a SNS; SNS hace POST a `/v1/sns/notification`; la app verifica la firma criptográfica de SNS y el ARN del topic, extrae el `user_id` del primer segmento de la clave y despacha `process_aws_upload.delay(bucket, key, user_id)`. La confirmación de suscripción se maneja sola.

**Delta de código (cuatro piezas):**

1. `aws_service.get_s3_client()` y `get_sns_client()` pasan las credenciales de `settings` sin condicional: si no hay llaves estáticas, `settings.aws_secret_access_key.get_secret_value()` lanza `AttributeError`, la función la captura y devuelve `None`. Con sólo el rol de la task, el endpoint de presigned devolvería 503 y la task de procesamiento fallaría. Hay que construir los kwargs de `boto3.client` condicionalmente, tal como ya lo hace `raw_payload_storage._create_s3_client`. Sin este cambio es imposible operar sin credenciales estáticas de AWS.
2. `POST /v1/sdk/users/{user_id}/sync/s3`: espejo del endpoint de XML, con `Content-Type: application/json` y clave `{user_id}/sdk/{batch_id}.json`. Debe tener tres segmentos, porque el parser de SNS exige al menos tres para extraer el `user_id`.
3. Task `process_s3_sdk_upload(bucket_name, object_key, user_id)`: baja el objeto y delega en el mismo servicio de import que usa `process_sdk_upload`, que recibe el contenido como string.
4. Despacho por prefijo en `sns_service`: `sdk/` a la task nueva, `raw/` al import de XML.

Del lado móvil, el SDK elige camino por tamaño: bajo ~1 MB va JSON directo al endpoint actual y conserva el 400/403 sincrónico; por encima pide la URL prefirmada. El backfill histórico cae siempre del lado de S3.

**Infra asociada:**

- Bucket de ingesta con SSE-KMS, block public access y política de TLS obligatorio, **separado** del bucket de payloads crudos (ciclos de vida y permisos distintos). Prefijos `{user}/sdk/` y `{user}/raw/`, lifecycle que expira objetos procesados a los 30 días — son PHI, no archivo histórico.
- Notificación del bucket a un topic SNS, con policy condicionada por `aws:SourceArn` y `aws:SourceAccount`.
- Suscripción HTTPS al endpoint de la API creada desde Terraform con `endpoint_auto_confirms`, ya que la app confirma sola.
- **DLQ (SQS) en la suscripción SNS.** Si el POST al endpoint falla de forma persistente, SNS reintenta y descarta. La redrive policy captura esas entregas. Es el único punto del flujo donde algo se perdería en silencio.
- URLs prefirmadas con vencimiento de 15 minutos y tope de tamaño en las condiciones del POST, para que el bucket no se convierta en storage gratuito.
- **El techo de tamaño es de 50 MB y lo impone el servidor**, no el cliente. El request puede pedir menos, nunca más. La razón no es S3 sino la memoria del worker, y el número sale de una medición, no de una estimación: un batch de 200 MB pica en ~2,0 GB residentes, porque el payload existe simultáneamente como bytes, como string decodificado, como dos árboles JSON parseados y como objetos pydantic validados. El multiplicador es ~10×, así que **la memoria del worker tiene que ser al menos diez veces el techo**. Importa porque un OOM es exactamente el caso de "worker muerto a mitad de la task": con `task_acks_late` el mismo payload se redelivera, vuelve a matar al worker y envenena la cola en loop.

**Permisos y credenciales:**

- Rol de task de la API: `s3:PutObject` sobre el bucket (para firmar) y `s3:ListBucket`, porque el servicio valida con `head_bucket` antes de firmar.
- Rol de task del worker: `s3:GetObject`.
- Una vez aplicada la pieza 1 del delta, `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY` **no se configuran** y boto3 toma el rol de la task. Sólo se definen `AWS_BUCKET_NAME`, `AWS_REGION` y `AWS_SNS_TOPIC_ARN` como variables planas.

**Idempotencia — parcialmente resuelto durante la implementación.** SNS entrega al menos una vez y el redelivery del broker suma un segundo camino, así que la misma task puede correr dos veces con el mismo objeto. Se trazó el código de import y el resultado es **mixto**:

| Tipo de dato | Idempotente | Mecanismo |
|---|---|---|
| Series temporales | Sí | `INSERT ... ON CONFLICT (data_source_id, series_type_definition_id, recorded_at) DO UPDATE`, con unique constraint `uq_data_point_series_source_type_time` |
| Workouts | Sí | `ON CONFLICT (data_source_id, start_datetime, end_datetime) DO NOTHING`, con índice único `ix_event_record_source_time`; los detalles se filtran a los ids realmente insertados |
| Sleep | **No confirmado** | No hay upsert a nivel de base: `handle_sleep_data` mantiene una sesión en Redis y `finish_sleep` busca el registro adyacente, lo borra y reconstruye uno fusionado. Hay una red de seguridad que colapsa intervalos duplicados exactos, pero no cubre payloads agregados sin intervalos de etapa ni las carreras de lock y TTL de Redis |

Consecuencia práctica: el reprocesamiento es seguro para series y workouts, y **riesgoso para sleep** — que es justo el insumo de los health scores. Antes de abrir el primer lote de backfill hay que cerrar esto con una prueba de reprocesamiento real sobre datos de sueño, no con lectura de código.

## 8. Migración desde Spike

Modalidad elegida: **re-vinculación de usuarios más backfill histórico**. Los tokens de Spike no son portables, así que cada usuario reconecta y el SDK empuja su historia.

Este es el evento dimensionante del sistema, no el régimen permanente:

- **Control de ritmo por cohortes desde la app móvil**, no desde la infraestructura. Se habilita la re-vinculación por lotes, se mide el primero y se calibra el resto. Con este control no hace falta una cola de amortiguación.
- Los backfills viajan por S3 (sección 7) y se procesan en la cola `sdk_sync`, atendida por `celery-worker-bulk` con autoscaling propio.
- Techo de ACU de Aurora elevado a 32 durante la ventana, y devuelto a 16 después.
- **Gate obligatorio antes del primer lote:** un guard de objeto ya procesado — clave `(user_id, object_key)` en Redis con `SET NX`, o una fila única equivalente — que haga del redelivery un no-op. Es lo único que cubre sleep, cuyo camino de escritura no es un upsert (sección 7). Deliberadamente **no** se implementó en la rama del delta de código: es un mecanismo nuevo que el plan no contemplaba, diverge del upstream, y un guard persistente saltearía en silencio los replays deliberados de los que depende el tooling de replay del propio fork. Se construye acá, con la decisión tomada a la vista.
- La exposición real de sleep no es el replay sino la concurrencia: el lock de Redis usa `timeout=30` y `blocking_timeout=15`, ambos superables con batches grandes, y una segunda task para el mismo usuario que no consigue el lock **descarta la porción de sueño y hace ack igual**. Pérdida silenciosa, no duplicación. `--concurrency=1` en el worker de backfill lo resuelve por construcción.

## 9. Observabilidad

**CloudWatch + Sentry**, sin Datadog.

El fork emite logging estructurado en JSON: cada batch del SDK registra `action`, `batch_id`, `user_id`, `provider` y los conteos de records, workouts y sleep. Con metric filters sobre esos campos se obtiene un embudo de ingesta sin instrumentación adicional.

Sentry ya está integrado (`log_and_capture_error`). Precauciones de compliance: `send_default_pii=false` y `LOG_ERROR_RESPONSE_BODY=false`, porque los cuerpos de error contienen datos de salud. Mandar PHI a Sentry lo convierte en subprocesador y exige un BAA que los planes estándar no incluyen: se lo trata como destino de stacktraces, nunca de payloads, y se verifica con una prueba explícita antes de prod.

**Alarmas:**

| Señal | Por qué |
|---|---|
| Profundidad de las colas de Celery | Sin Flower, es el indicador primario de saturación. Requiere un Lambda de un minuto haciendo `LLEN` y publicando métrica custom (en VPC, con SG hacia ElastiCache) |
| Memoria de ElastiCache > 70% | Con `noeviction`, al 100% fallan las publicaciones de tasks |
| Conexiones de Aurora vs límite | En Serverless v2 el `max_connections` escala con las ACU; api con autoscaling más workers con pool de threads pueden agotarlo. Si nos acercamos al límite, la respuesta es RDS Proxy |
| Batches recibidos = 0 por 30 min | Detecta "el SDK dejó de poder entrar" (regla de WAF, certificado, DNS). Ninguna métrica de infra lo muestra |
| Ninguna task programada en N minutos | Liveness de `celery-beat`: un beat muerto no genera errores, sólo silencio |
| Mensajes en la DLQ de SNS > 0 | Backfills que se perderían sin aviso |
| Exit code ≠ 0 de la task de migración | Aborta el deploy; hay que verlo, no descubrirlo después |
| 5xx y p99 del ALB, requests bloqueados por WAF, running count < desired | Base operativa |

**Canary de Synthetics** cada 5 minutos desde fuera de la VPC contra `GET /`: es lo único que detecta una mala configuración de WAF o de certificado antes que los usuarios.

**Evidencia de auditoría:** CloudTrail, AWS Config, retención de logs definida por grupo, y logging de sesiones de `ecs execute-command` — el control que reemplaza al bastión.

## 10. Costos estimados

us-east-1 para prod, orden de magnitud, a refinar con volúmenes reales.

| Concepto | prod / mes | qa / mes |
|---|---|---|
| Aurora Serverless v2 | $300 – 500 | $90 |
| Fargate (5 servicios) | $170 | $60 |
| ElastiCache | $50 | $12 |
| VPC endpoints (5 interface × AZ) | $110 | $37 |
| ALB + WAF | $40 | $30 |
| CloudWatch (logs, alarmas, canary) | $75 | $20 |
| S3, KMS, ECR, Secrets | $25 | $10 |
| **Total** | **~$790 – 990** | **~$260** |

Aurora es la línea dominante y la más variable: depende directamente de la decisión de granularidad de la sección 6.

**Palancas de ahorro:** `qa` puede pausarse — Serverless v2 admite mínimo 0 ACU con auto-pause y los servicios ECS pueden bajar a 0 tasks fuera de horario con scheduled scaling, lo que deja `qa` en unos $80/mes. Los VPC endpoints de `qa` van en una sola AZ.

**Dato pendiente para el caso de negocio:** el costo actual de Spike. Si es del orden de estas cifras, el argumento es soberanía y control del dato; si es varias veces mayor, el ahorro paga el proyecto.

## 11. Layout en longevoIac

Proyecto nuevo `src/open-wearables`, plano en la raíz de `src/`, independiente del `src/longevo/wearables` existente (que conserva su DocumentDB y su ingesta de wearable propietario). Slug de CI: `open-wearables`. GitHub environments: `qa-open-wearables` y `prod-open-wearables`.

No va anidado bajo `src/longevo/`. Esa nesting existe en el repo (`longevo/wearables`, `doctorsv/wearables`) porque el mismo nombre de proyecto tuvo que existir una vez por marca de cliente — los dos se crearon el mismo día. `open-wearables` no tiene esa colisión, y todo lo creado después en el repo está plano en la raíz.

```
src/open-wearables/
├── modules/
│   ├── constants/          # project = "open-wearables"
│   ├── network/            # VPC, subnets, endpoints, flow logs
│   ├── platform/           # cluster ECS, ALB, WAF, ACM
│   ├── data/               # Aurora, ElastiCache
│   ├── service/            # servicio ECS genérico sobre src/modules/ecs/base
│   └── sdk-ingest/         # bucket, SNS, DLQ, notificación
└── environments/
    ├── qa/
    └── prod/
```

Se reutilizan los módulos compartidos `src/modules/ecs/base`, `src/modules/s3` y `src/modules/sqs`. Nomenclatura de recursos: `${env}-open-wearables-<name>`, siguiendo la convención de `ecs/base`.

**Orden de implementación sugerido.** Todo se construye primero en `qa` y se replica en `prod` recién al final, siguiendo la recomendación de longevoIac de un ambiente por PR:

1. `network` en qa (resuelto V3).
2. `data` en qa: Aurora con sus tres usuarios y ElastiCache con el parameter group correcto (resuelto V1).
3. Delta de código en el fork: credenciales de boto3 por rol de task, extracción de `init.sh` y ajustes de configuración de Celery (`task_acks_late`, `prefetch`). Es independiente de la infra y se valida con la suite de tests y `docker compose`.
4. `platform` en qa (cluster, ALB, WAF, ACM) más el pipeline de deploy del fork y el rol OIDC.
5. `service` en qa: los cinco servicios, con la task de migración corriendo en el pipeline.
6. `sdk-ingest` en qa: bucket, SNS, DLQ, y el delta de código de la sección 7. Validar V2 acá.
7. Observabilidad y alarmas en qa.
8. Replicar 1–7 en prod, un módulo por PR.
9. Cohortes de migración: primer lote acotado, medición, calibración.

## 12. Ítems de verificación

Bloqueantes antes de la implementación de la parte correspondiente:

| # | Ítem | Bloquea |
|---|---|---|
| V1 | ~~¿ElastiCache ofrece Redis 8?~~ **Resuelto:** no. Redis OSS corta en 7.1, Valkey llega a 9.1. Se va a Valkey (ver sección 6) | — |
| V2 | Idempotencia ante entrega duplicada. **Parcialmente resuelto:** series y workouts sí, sleep no confirmado (ver sección 7). Falta una prueba de reprocesamiento real sobre datos de sueño | Primer lote de backfill |
| V3 | ~~¿Qué CIDRs usan hoy las VPCs de Longevo?~~ **Resuelto:** `172.31.0.0/16` en prod y qa (VPCs default), `10.10.0.0/16` en staging, más los pools VPN `10.100/10.150/10.200.0.0/22`. `10.60`/`10.61` confirmados (ver sección 4) | — |

No bloqueantes, pero necesarios para cerrar el sizing:

| # | Ítem |
|---|---|
| V4 | Filas de `data_point_series` por usuario-día, para dimensionar Aurora y decidir la granularidad |
| V5 | Cuántas llamadas de sync del SDK corresponden a los 400k eventos/día de Spike (no es 1:1: menos llamadas con payloads mayores) |
| V6 | Cantidad de usuarios a migrar y ventana disponible, para calibrar las cohortes |
| V7 | Costo actual de Spike |
| V8 | Dominio y subdominios para `api.*` y `app.*`, y quién administra Route 53 y ACM |
| V9 | ¿Cuenta AWS separada para wearables, o VPC separada dentro de la cuenta actual? El aislamiento de red ya está; el de cuenta no. Decisión de compliance con impacto en Organizations, facturación y OIDC |

## 13. Mapeo de controles HIPAA / ISO 27001

| Control | Implementación |
|---|---|
| Cifrado en reposo | CMK de KMS por ambiente en Aurora, ElastiCache, S3, logs |
| Cifrado en tránsito | TLS en el ALB, `rds.force_ssl=1`, transit encryption en ElastiCache, política de bucket con `aws:SecureTransport` |
| Segmentación de red | VPC dedicada sin peering, subnets aisladas para datos, SGs en cadena, sin egress a internet |
| Control de acceso | Roles de task por servicio con permisos mínimos, tres usuarios de base con separación migrador/runtime, OIDC sin llaves de larga vida |
| Gestión de secretos | Secrets Manager con rotación; sin credenciales estáticas de AWS en la app (rol de task) |
| Minimización de PHI | Lifecycle de 30 días en payloads crudos, `LOG_ERROR_RESPONSE_BODY=false`, redacción de `Authorization` en logs de WAF, Sentry sin PII |
| Auditoría | CloudTrail, AWS Config, VPC Flow Logs, logs de ALB y WAF, logging de sesiones de `ecs execute-command`, pgaudit de DDL y roles |
| Continuidad | PITR 35 días, backups cross-region, Multi-AZ en Aurora y ElastiCache, `deletion_protection` |
| Integridad de datos | `noeviction` en ElastiCache, `task_acks_late`, DLQ de SNS, idempotencia verificada |

## 14. Evolución posterior a fase 1

- **Providers OAuth:** requiere NAT Gateway (tráfico de salida a APIs de providers), endpoints de webhook entrantes en el WAF, credenciales por provider en Secrets Manager, y reactivar `sync-all-users-periodic` con throttling por rate limit de cada provider.
- **Buffer de ingesta con SQS:** camino documentado para cuando la ingesta sostenida supere ~500 rps o aparezca el requisito de no perder eventos durante ventanas de deploy. La entrada natural es delante de los webhooks de providers, no del SDK.
- **Escalado del worker por profundidad de cola:** promover la métrica custom de la sección 9 a política de autoscaling.
- **Svix y webhooks salientes:** servicio ECS propio más base `svix` dedicada, cuando haya consumidores externos.
- **RDS Proxy:** si las conexiones de Aurora se acercan al límite.
- **stg:** tercer ambiente cuando el ciclo qa → prod deje de alcanzar.

## 15. Referencias al código

| Tema | Ubicación |
|---|---|
| Endpoint de sync del SDK | `backend/app/api/routes/v1/sdk_sync.py` |
| Presigned S3 e ingesta por SNS | `backend/app/api/routes/v1/import_xml.py`, `backend/app/services/apple/apple_xml/presigned_url_service.py`, `backend/app/services/apple/apple_xml/sns_service.py` |
| Task de procesamiento desde S3 | `backend/app/integrations/celery/tasks/process_aws_upload_task.py` |
| Configuración de Celery | `backend/app/integrations/celery/core.py` |
| Inicialización y migraciones | `backend/scripts/start/app.sh`, `backend/scripts/init/create_svix_db.py` |
| Health check | `backend/app/main.py:77` |
| Settings y flags | `backend/app/config.py`, `backend/config/.env.example` |
| Frontend SSR | `frontend/Dockerfile`, `frontend/vite.config.ts` |
| Convenciones de Terraform | `longevoIac/src/README.md`, `longevoIac/src/modules/ecs/base` |
