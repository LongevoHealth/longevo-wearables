# Ingesta de Open Wearables hacia la API de dominio de Longevo

**Fecha:** 2026-09-11
**Estado:** aprobado para pasar a plan de implementación
**Alcance:** fase 1 — reemplazo de Spike como fuente de eventos y datos de wearables, manteniendo intacto el flujo de dominio de Longevo

Complementa a [2026-08-22-aws-infra-design.md](./2026-08-22-aws-infra-design.md), que definió cómo se despliega el fork en AWS y dejó explícitamente abierto cómo lo consume el resto de Longevo.

---

## 1. Contexto y objetivo

Longevo consume hoy datos de wearables a través de Spike. El diseño de infraestructura de agosto definió el despliegue del fork de Open Wearables en AWS pero dejó el consumo resuelto sólo a grandes rasgos: *"ALB público con WAF y API key"*. Este documento cierra esa interfaz.

La pregunta original era si convenía usar el mecanismo de webhooks salientes que el fork ya trae, o si —al vivir los dos sistemas en la misma cuenta de AWS— convenía que Longevo leyera directamente de la base de Open Wearables, con una notificación liviana que sólo avisara "hay dato nuevo para este usuario".

**La respuesta corta es que la segunda intuición es correcta en su forma y equivocada en su medio.** Notificación fina más pull es exactamente el patrón que Longevo ya corre contra Spike y que funciona en producción. Pero el pull tiene que ir contra una API versionada, no contra tablas, y la notificación tiene que viajar por el bus nativo de AWS, no por HTTP.

## 2. Alcance de fase 1

**Dentro:**

- Un evento coalescido `sync.completed` emitido por Open Wearables al terminar cada corrida de ingesta.
- **Los siete providers habilitados**, agrupados por camino de ingesta y por lo que cada grupo cuesta enriquecer (V0, resuelto — ver sección 4.1).
- Transporte por SNS → SQS FIFO, dentro de la misma cuenta de AWS.
- Pull de detalle desde `wearables-api` contra la External API del fork.
- Convivencia con Spike durante la migración, por cohortes y con rollback.

**Fuera:**

- **Cambiar qué persiste Longevo.** El store de `wearables-v2` en DocumentDB, el matching de actividades, los resúmenes diarios B2B y la publicación de user actions quedan **exactamente como están**. Este cambio reemplaza la fuente, no el dominio.
- Svix y los webhooks salientes del fork, que siguen deshabilitados (sección 2 del diseño de infra).
- Scopes en las API keys del fork (ver sección 6, se resuelve en el borde).
- **Garmin**, hasta que existan las credenciales. El diseño lo contempla (sección 4.1) pero no se implementa ahora.
- Providers que Longevo no piensa habilitar (Polar, Suunto, Fitbit, Ultrahuman). **No es una restricción de infraestructura:** el NAT Gateway y el ingress/egress de providers ya están habilitados, lo que **supersede la decisión D7 del diseño de infra de agosto** ("Sin NAT Gateway en fase 1"). No confundir con el D7 de este documento. Cada provider entra cuando existe su cuenta y su `metadata` propaga la ventana.

## 3. Decisiones y justificación

| # | Decisión | Justificación |
|---|---|---|
| D1 | Notificación fina + pull, no payload completo | Es el patrón vigente contra Spike (`SpikeWebhookEventDto` no trae dato de salud). No se inventa nada: se reimplementa algo que ya opera |
| D2 | **No** leer la base de Open Wearables desde Longevo | Tres razones independientes, cualquiera alcanza: no hay marca de agua de ingesta (`data_point_series` y `event_record` no tienen `created_at` ni `updated_at`), la resolución de prioridad entre fuentes vive en el service layer y no en la base, y acopla el dominio de Longevo al esquema de un fork que resincronizamos con upstream |
| D3 | Transporte por SNS → SQS, no por HTTP/Svix | Misma cuenta de AWS: autenticación por IAM en vez de secreto compartido, reintentos y DLQ sin código, y ningún endpoint público nuevo. Svix implica un servicio ECS más y una base dedicada que el diseño de infra sacó de fase 1 |
| D4 | Evento coalescido por batch, no por registro | El fork emite hoy por tipo de dato: `on_timeseries_batch_saved` dispara dos eventos por cada series_type (grupo + granular), más uno por sesión de sueño y por workout. Un sync típico produce ~23 eventos contra 1 de Spike. Sin coalescer, es 23× de cola y 23× de pulls por la misma ventana |
| D5 | El coalescedor va en el fork, no en Longevo | Debouncear del lado del consumidor implica absorber los 23 eventos igual y resolver una carrera |
| D5b | La emisión va en `sync_status_service.emit_event`, no en `process_sdk_upload` | Los cuatro caminos de ingesta del fork (SDK, webhook de provider, polling, import histórico) desembocan en `emit_event` vía `completed()` o `webhook_delivered()`. Es el único punto por el que pasa "una corrida terminó" para cualquier provider. Emitir en el task del SDK ataría el evento a un camino y obligaría a un segundo productor en fase 2 |
| D6 | Cola FIFO con `MessageGroupId = external_user_id` | Serializa por usuario sin perder paralelismo entre usuarios. Necesario por el snapshot-replace de sueño (sección 7), y de paso da deduplicación por `batch_id` |
| D7 | El evento lleva los dos identificadores | `external_user_id` matchea contra `user_wearables` sin tocar el mapeo actual; el UUID interno arma la URL del pull. Mandar sólo uno cuesta un round-trip extra por evento |
| D8 | Restricción de método por WAF en vez de scopes en la API key | En fase 1 Longevo es el único consumidor de la External API, así que la regla de borde cubre el caso con cero divergencia contra upstream. El flag read-only queda para cuando aparezca un segundo consumidor |
| D9 | El store de Longevo no se toca en fase 1 | Cambiar fuente y modelo de persistencia a la vez duplica la superficie de riesgo de la migración sin necesidad. La discusión de store contra cache se da en fase 2, con métricas reales |

## 4. El evento

### Punto de emisión

El fork tiene **cuatro caminos de ingesta**, no uno:

| Camino | Task | Providers |
|---|---|---|
| SDK | `process_sdk_upload_task` | Apple Health, Health Connect |
| Webhook de provider | `process_webhook_push` | Garmin, Suunto, Oura, Strava, Polar, Whoop |
| Polling | `sync_vendor_data_task` | providers sin webhook |
| Import histórico | `process_xml_upload_task`, `garmin/backfill_task` | XML de Apple, backfill de Garmin |

Los cuatro terminan en `sync_status_service`: los tres primeros por `completed(...)`, el de webhooks por `webhook_delivered(...)`, y **ambas funciones desembocan en `emit_event(...)`**. Ese es el único punto del fork por el que pasa "una corrida de ingesta terminó", sea cual sea el provider.

**La emisión va ahí**, en `emit_event`, filtrada por `stage == COMPLETED` y `status` exitoso —lo que descarta los `SKIPPED` que `webhook_delivered` usa para entregas duplicadas o no-op, y los eventos de `started` y `progress`—. Un solo productor cubre todos los providers, presentes y futuros.

**Lo que sí es por camino es el enriquecimiento del `metadata`.** `emit_event` recibe `user_id`, `provider`, `source`, `run_id`, `items_processed` y un `metadata` libre que cada llamador arma distinto. Hoy:

- `process_sdk_upload` ya pasa `types` —la lista de slugs de series que tocó el batch, equivalente exacto de `metrics[]` de Spike—, `batch_id` y los tres conteos. **Le falta sólo la ventana temporal**: `_load_data` no trackea el mínimo y máximo de `recorded_at`. Son dos variables en un loop que ya recorre todas las muestras.
- `webhook_push_task` pasa únicamente `{inserted, updated}`. **Enriquecerlo es trabajo de fase 1**, no de fase 2, porque el camino ya está vivo. Los handlers de provider tienen el dato: el de Whoop, por ejemplo, ya trae el registro completo desde la API del provider en `_handle_updated`, pero devuelve sólo `{status, event_type, records_saved}` y descarta el rango temporal. Propagarlo es pasar dos campos más hacia arriba.

Un evento sin ventana se emite igual, con los campos en `null`. **El consumidor lo descarta con un warning en vez de puleear a ciegas** — un pull sin ventana contra un usuario con años de historia es un incidente, no un fallback. Eso hace que habilitar un camino nuevo sea explícito: hasta que su `metadata` no traiga la ventana, ese provider no llega al dominio de Longevo.

### Contrato

```json
{
  "event_type": "sync.completed",
  "schema_version": 1,
  "user_id": "<uuid interno de Open Wearables>",
  "external_user_id": "<userWearableId de Longevo>",
  "provider": "apple",
  "batch_id": "<uuid>",
  "occurred_at": "2026-09-11T14:03:12Z",
  "metrics": ["steps", "heart_rate", "heart_rate_variability_sdnn"],
  "activity_types": ["sleep", "running"],
  "earliest_record_start_at": "2026-09-10T22:14:00Z",
  "latest_record_end_at": "2026-09-11T13:58:00Z",
  "counts": { "records": 1240, "workouts": 2, "sleep": 1 }
}
```

Cero dato de salud: sólo qué cambió y dónde buscarlo. Es la forma de `SpikeWebhookEventDto` más `batch_id` y `schema_version`.

`external_user_id` es nullable en el modelo `User` del fork — un usuario creado desde el panel no lo tiene. **El evento se emite igual con el campo en `null` y Longevo lo descarta con un warning**, siguiendo el camino que hoy tiene el `BAD user client id`. Emitir siempre y filtrar en el consumidor es preferible a un productor que decide en silencio no avisar.

### 4.1 Inventario de providers y superficie de enriquecimiento

Los siete providers habilitados caen en tres grupos. El trabajo **no es por provider sino por grupo**:

| Grupo | Providers | Camino | Enriquecimiento |
|---|---|---|---|
| **SDK** | Apple Health, Health Connect, Samsung Health | `process_sdk_upload_task` | **Uno solo para los tres.** `_get_import_service` devuelve el mismo servicio para `apple`, `google` y `samsung`; la ventana se calcula una vez en `_load_data`. `types` y los conteos ya viajan |
| **Webhook notify** | Whoop, Strava, Oura | `process_webhook_push` | **Tres handlers, uno por provider.** Cada `process_payload` devuelve una forma distinta y ninguno propaga el rango temporal del registro que acaba de traer |
| **Garmin** | Garmin | handlers propios + `backfill_task` | **Diferido.** Sin credenciales todavía. Ver la nota de abajo: es el que rompe el supuesto de un evento por corrida |

Tres cosas que salieron de mapearlo y que conviene tener a la vista:

**Whoop y Strava borran; Oura no.** Los handlers de Whoop y Strava implementan borrado (`_handle_deleted`, `records_deleted`), así que producen un evento terminal que el snapshot-replace convierte en borrado del lado de Longevo. **El de Oura descarta el evento de borrado explícitamente** (`{"status": "ignored", "reason": "delete_event"}`), que `webhook_push_task` mapea a `SKIPPED` y por lo tanto no emite nada.

Consecuencia, y es la correcta: **Longevo va a espejar fielmente a Open Wearables, incluido ese hueco.** Si Oura borra un registro, Open Wearables lo conserva y Longevo también. No es un defecto de esta integración ni algo que se arregle del lado de Longevo — es una brecha del fork contra el provider, y el lugar de arreglarla es upstream. Se documenta acá para que nadie la debuguee desde el consumidor.

**Garmin no emite un evento por corrida, emite cinco.** Hay cinco llamadas a `completed()` —dos en los handlers de `activities` y `wellness`, tres en `backfill_task`— cada una con su propio `new_run_id`. Es coherente con el modelo de `sync_status` (cada tipo de dato es una corrida propia), pero significa que un push de Garmin puede generar varios eventos y por lo tanto varios pulls sobre ventanas solapadas. Con la cola FIFO ordenada por usuario eso es correcto aunque redundante. Es el ítem V4, y **Garmin es el provider que lo va a activar**: conviene resolverlo antes de habilitarlo, no después.

**El primer entregable cubre cuatro de los siete.** El enriquecimiento del grupo SDK más el de Whoop deja a Apple, Health Connect, Samsung y Whoop llegando al dominio. Strava y Oura son dos handlers más de la misma forma, y Garmin espera credenciales.

### Borrados

Los providers OAuth **borran registros**, no sólo los crean: el handler de Whoop implementa `_handle_deleted`, y `webhook_push_task` cuenta `deleted` entre los estados que ya producen un evento terminal. El contrato dice "hay dato nuevo", que no cubre "se fue un dato".

No hace falta un tipo de evento aparte. **El snapshot-replace por ventana (sección 7) resuelve el borrado gratis**: si el evento del borrado trae la ventana del registro eliminado, Longevo reemplaza esa ventana con lo que la External API devuelva —que ya no incluye el registro— y desaparece solo.

Esto reordena la justificación del snapshot-replace: deja de ser un parche para la recreación de sueño del SDK de Apple y pasa a ser **la propiedad que hace correcta toda la integración**. Con upsert-por-id, un borrado en Open Wearables quedaría para siempre en DocumentDB, sin que nada lo detecte.

### Ubicación en el fork

Módulo nuevo `app/services/outgoing_webhooks/sync_notifications.py`, al lado de `svix.py`, con un publisher a SNS detrás de dos flags: `SYNC_NOTIFICATIONS_ENABLED` y `AWS_SYNC_EVENTS_TOPIC_ARN`. **No toca el camino de Svix**, que queda deshabilitado como lo dejó el diseño de infra. El día que se prenda para un consumidor externo, los dos conviven.

boto3 y SNS ya son parte del fork por la ingesta desde S3, así que no es tecnología nueva ni un patrón ajeno al upstream.

## 5. Los pulls

| Hoy contra Spike | Contra Open Wearables |
|---|---|
| `getDailyStatistics({from, to})` | `GET /api/v1/users/{id}/summaries/activity?start_date&end_date` |
| `getSleeps({fromDate, toDate})` | `GET /api/v1/users/{id}/events/sleep?start_date&end_date&filter_by_priority=true` |
| `getWorkouts({fromTimestamp, toTimestamp})` | `GET /api/v1/users/{id}/events/workouts?start_date&end_date` |
| `getSpikeWorkoutById(id)` | **No requiere pull.** Lee de DocumentDB con el ID propio de Longevo, y en fase 1 el store se mantiene |

`filter_by_priority` en el endpoint de sueño hace exactamente el desempate entre fuentes que `buildSpikeSummaries` implementa hoy a mano.

La librería `openwearables-sdk` del monorepo (`monorepo-backend/libs/openwearables-sdk`, rama `feat/openwearables-integration`) ya cubre los tres pulls contra los endpoints correctos, ya tiene `findUserByExternalId`, y `fetchAllPages` ya sigue `pagination.next_cursor` hasta agotar. **La paginación no es trabajo pendiente.**

## 6. Autenticación y frontera de permisos

Dos credenciales, ninguna compartida.

**El evento (Open Wearables → Longevo).** IAM puro. El rol de task del worker de OW obtiene `sns:Publish` sobre un único topic; la cola de Longevo se suscribe con una policy condicionada por `aws:SourceArn` y `aws:SourceAccount`. No hay secreto que rotar. El controller `POST /webhook/:longevoApiKey` de `wearables-api` deja de recibir tráfico para este flujo: una superficie pública menos.

**El pull (Longevo → Open Wearables).** API key en el header `X-Open-Wearables-API-Key`, en Secrets Manager.

**Restricción de escritura.** El modelo `ApiKey` del fork no tiene scopes: tiene `id`, `name` y `created_by`. Y `ApiKeyDep` protege también endpoints que mutan — `create_user`, alta y baja de conexiones, `sync_data`, `import_xml`. Una key es hoy, de hecho, una credencial de escritura.

En fase 1 esto se cierra **en el borde y sin código**, pero **no con una regla de sólo-lectura**. Esa fue la primera versión de este diseño y era inaplicable: Longevo escribe legítimamente contra la External API — `POST /api/v1/users` y `POST /api/v1/users/{id}/token` ocurren en `createSession`, o sea en la primera llamada de la integración; `PATCH /api/v1/users/{id}` actualiza el perfil; y `DELETE /api/v1/users/{id}/connections/{provider}` desvincula un provider desde la app. Una regla de sólo-GET habría roto las cuatro.

El control correcto no es "sólo lectura" sino **"nada destructivo"**, que además es la traducción literal del requisito. La regla bloquea las operaciones que Longevo nunca ejecuta y cuyo daño es irreversible:

| Bloqueado | Permitido |
|---|---|
| `DELETE /api/v1/users/{id}` — borra el usuario entero | `DELETE /api/v1/users/{id}/connections/{provider}` — desvincular, que la app sí hace |
| `DELETE /api/v1/users/{id}/connections/{provider}/data` — borra los datos de un provider | `POST /api/v1/users`, `PATCH /api/v1/users/{id}`, `POST /api/v1/users/{id}/token` |
| `DELETE /api/v1/users/{id}/events/...` — borra registros individuales | `POST /api/v1/users/{id}/import/apple/xml/*` — ingesta real |

Se define en Terraform junto al resto del WAF y queda documentado ahí.

**Fase 2, cuando aparezca un segundo consumidor:** un booleano `read_only` en `ApiKey`, su migración aditiva, y un chequeo dentro de `_require_api_key` que rechace todo lo que no sea `GET` cuando la key es de lectura. `_require_api_key` es un único punto de paso con acceso al request, así que **no hay que anotar ningún endpoint**: un archivo de divergencia contra upstream, no doce.

**Frontera resultante:** Longevo puede recibir de una cola y leer por HTTP. No puede escribir en Aurora, disparar syncs, ni crear o borrar usuarios y conexiones. No tiene ruta de red a la base. La diferencia con leer la base directamente es que esto no depende de que nadie se porte bien.

## 7. Idempotencia y fallos

### El hallazgo bloqueante: el ID de sueño de Open Wearables no es estable

Los writers de Longevo hacen `findOneAndUpdate` con `upsert: true` matcheando por un ID estable del proveedor: `sleep_id` para sueño, `record_id` para workouts. Con Spike eso hace que reprocesar sea un no-op.

Con Open Wearables no se cumple. En [`sleep_service.py:425`](../../../backend/app/services/apple/healthkit/sleep_service.py), `finish_sleep` busca un registro de sueño adyacente, **lo borra** y crea uno nuevo con `id=uuid4()`. Su propio docstring aclara que no es un caso raro sino el patrón normal del SDK de Apple, que manda una noche como muchos payloads consecutivos y extiende el registro acumulado en cada merge.

La cadena de consecuencias:

1. Una noche llega en 6 payloads → 6 batches → 6 eventos `sync.completed`.
2. Longevo pulea `/events/sleep` seis veces y recibe un UUID distinto cada vez para la misma noche.
3. El writer matchea por `sleep_id`; UUID nuevo significa documento nuevo.
4. Una noche termina como 6 documentos en DocumentDB.

Este es el ítem **V2 del diseño de infra de agosto** ("sleep: no confirmado"), ahora con consecuencia medible en el consumidor. No confundir con el V2 de la sección 13 de este documento.

**Resolución: snapshot-replace por ventana.** En cada pull, el writer de Open Wearables borra los sueños del usuario en el rango de fechas puleado e inserta lo que vino. Es lo semánticamente correcto para un modelo de pull por ventana —la respuesta *es* el estado autoritativo de esa ventana— está contenido en un writer nuevo del lado de Longevo, y no requiere tocar el fork. Elimina además el heurístico de "quedarse con el más reciente por fecha" que `buildSpikeSummaries` usa hoy para lidiar con noches duplicadas.

### La carrera que abre, y su cierre

El consumidor actual usa cola standard con `batchSize: 10`, así que puede procesar diez mensajes en paralelo. Con upsert-por-id eso era inofensivo; con delete-then-insert, dos eventos del mismo usuario con ventanas superpuestas se pisan.

**Cola FIFO con `MessageGroupId = external_user_id`.** Ordena estrictamente por usuario y mantiene el paralelismo entre usuarios distintos. Con `MessageDeduplicationId = batch_id` cubre además el reintento de SNS dentro de la ventana de 5 minutos. Es un cambio de infraestructura, no de código de negocio.

### Fallos

- **DLQ** después de N recepciones, con alarma sobre profundidad. Ya está previsto en la sección 9 del diseño de infra. El handler actual ya hace `throw` para que SQS reintente, así que no cambia.
- **Pull vacío por visibilidad.** El evento podría llegar antes de que la transacción de Open Wearables sea visible. El punto de emisión propuesto está después del commit de `import_data_from_request`, pero el manejo de sueño ocurre después de ese commit: **hay que confirmar dónde queda la visibilidad real antes de dar el orden por cerrado** (ítem V2 de la sección 13). Si existe ventana, se trata como reintento acotado, nunca como éxito silencioso.

## 8. Convivencia y migración desde Spike

El mecanismo de convivencia **ya existe y está probado en producción**. `DefaultWearableSourceService` resuelve por usuario cuál fuente manda, con caché de 15 minutos en Valkey y un `invalidate()`. `PublishWearableDaySummaryUseCase` chequea `defaultSource !== source` y se saltea, y el comentario del enum `WearableSourceType` es explícito: sólo la fuente default de un usuario puede publicar user actions. Hoy se usa para desempatar entre el wearable propietario y Spike.

La migración consiste en:

1. Agregar `OPEN_WEARABLES` al enum `WearableSourceType`.
2. Una rama en `resolve()`, con precedencia **PROPRIETARY > OPEN_WEARABLES > SPIKE**.
3. El predicado de esa rama es **"el usuario tiene Open Wearables produciendo"**, no "tiene sesión creada". Concretamente: una marca en `user_wearables` que se setea al procesar el **primer `sync.completed` exitoso** del usuario. `POST /session` es idempotente y mobile lo llama de forma recurrente, así que la existencia del `openWearablesUserId` no alcanza como predicado — prueba que la sesión existe, no que haya dato. Con la marca, Spike puede seguir mandando eventos y simplemente deja de emitir hacia abajo.
4. Llamar a `invalidate(userId)` al setear esa marca, porque si no el switch tarda hasta 15 minutos en verse.

Esa misma marca es el disparador de la baja de conexiones de Spike descrita más abajo: un solo hecho observable gobierna el cambio de fuente, la compuerta de escritura y la baja.

Esto da **migración por cohortes sin feature flags nuevos y rollback sin deploy**.

**Escritura de una sola fuente por usuario.** Durante el solapamiento, las dos fuentes escribirían en el mismo store con claves distintas (`sleep_id` de Spike contra el UUID de OW), produciendo documentos duplicados por la misma noche. Se cierra con la misma compuerta: **el handler de Open Wearables escribe sólo si OW es la fuente default de ese usuario**. Una sola compuerta gobierna emisión y escritura.

### Baja progresiva de conexiones de Spike

Objetivo económico: dejar de pagar MAU de Spike a medida que los usuarios migran.

**Disparador: el primer `sync.completed` exitoso del usuario**, no la creación de la sesión. `POST /session` en `openwearables.controller.ts` es idempotente y mobile lo llama de forma recurrente, así que dispararía todo el tiempo; y sobre todo **no prueba que Open Wearables esté produciendo dato**. Dar de baja Spike ahí deja al usuario sin ninguna de las dos fuentes si la vinculación falla después.

**Ejecución asíncrona.** La baja se encola, no se hace inline: una caída de Spike no puede bloquear la migración de un usuario. Debe ser idempotente y tolerar que la conexión ya no exista.

**Lo que no se borra:** la conexión, no la historia. Los documentos ya persistidos en `wearables-v2` quedan intactos, así que el histórico del usuario y `getSpikeWorkoutById` siguen funcionando.

**Costo de esta decisión, explícito:** dar de baja la conexión de Spike cierra el camino de rollback automático descrito arriba — un usuario migrado ya no puede volver a Spike sin reautorizar con el proveedor. El impacto económico de diferirlo es bajo (Spike factura por mes y la validación de una cohorte tarda días), así que **la baja se ejecuta al validar la cohorte, no al migrar el usuario**. Se preserva el rollback durante toda la ventana de riesgo y el ahorro cae en el mismo mes de facturación.

## 9. Deltas de código

### Fork (`longevo-wearables`)

| # | Cambio | Tamaño |
|---|---|---|
| 1 | `sync_notifications.py`: publisher a SNS detrás de flags | Archivo nuevo |
| 2 | Emisión en `sync_status_service.emit_event`, filtrada por stage terminal y status exitoso | ~10 líneas |
| 3 | Trackeo de mínimo y máximo de `recorded_at` en `_load_data`, y su paso al `metadata` de `completed(...)` | 2 variables + 1 campo |
| 4 | `external_user_id` en la respuesta del import, para armar el evento | Trivial |
| 5 | Propagar la ventana temporal en los handlers de provider webhook ya habilitados, hacia el `metadata` de `webhook_delivered` | Por provider, chico |

Ninguno toca el camino de Svix ni el de ingesta. Los cuatro tienen forma de upstream y el 1 es contribuible tal cual.

### Monorepo (`monorepo-backend`)

| # | Cambio |
|---|---|
| 1 | DTO `OpenWearablesSyncEventDto` y handler SQS para la cola nueva |
| 2 | Use cases análogos a `HandleActivityRecordUseCase` / `HandleSleepRecordUseCase` / `HandleStepsRecordUseCase`, apoyados en `openwearables-sdk` |
| 3 | Mappers de las entidades de Open Wearables hacia las que ya consumen los use cases |
| 4 | Writer de sueño con snapshot-replace por ventana (sección 7) |
| 5 | `OPEN_WEARABLES` en `WearableSourceType` y rama en `DefaultWearableSourceService.resolve()` |
| 6 | Compuerta de escritura por fuente default en el handler |
| 7 | Baja diferida de conexiones de Spike, encolada e idempotente |

### Infraestructura (`longevoIac`)

| # | Cambio |
|---|---|
| 1 | `src/open-wearables/environments/qa/sync-events.tf`: topic SNS FIFO, CMK y `sns:Publish` en el rol de task del worker |
| 2 | `src/longevo/wearables/environments/qa/open-wearables-sync-events.tf`: cola FIFO sobre `modules/sqs`, queue policy, suscripción y permisos de consumo en el rol de task |
| 3 | Regla de método y path en el WAF existente de `open-wearables` |
| 4 | Secreto de la API key de lectura, con rotación |

Detalle de propiedad y referencias cruzadas en la sección 10.

## 10. Infraestructura — `longevoIac`

El puente cruza **dos proyectos de Terraform**, y eso obliga a decidir quién declara qué.

### Quién posee cada pieza

El precedente en el repo es `modules/proprietary-wearable-raw-ingest`, instanciado desde `longevo/wearables/environments/{qa,stg,prod}/`: **el proyecto del consumidor posee la cola y su policy**, aunque el productor sea otro sistema. Se sigue el mismo reparto.

| Pieza | Proyecto | Archivo |
|---|---|---|
| Topic SNS FIFO + CMK + `sns:Publish` en el rol de task del worker | `src/open-wearables/` | `environments/<env>/sync-events.tf` (nuevo) |
| Cola SQS FIFO + DLQ + queue policy + suscripción al topic | `src/longevo/wearables/` | `environments/<env>/open-wearables-sync-events.tf` (nuevo) |
| `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` en el rol de task de `wearables-api` | `src/longevo/wearables/` | mismo archivo |
| Regla de WAF por método y path (sección 6) | `src/open-wearables/` | `environments/<env>/dns-alb-waf.tf` (existente) |
| Secreto con la API key de lectura | `src/longevo/wearables/` | `environments/<env>/secrets.tf` |

**La suscripción va con la cola, no con el topic.** Es lo que hace que dar de baja el consumidor sea una operación de un solo proyecto.

### Cómo se referencian entre proyectos

No hay estado remoto compartido en el repo. El único precedente de referencia cruzada es `longo/environments/prod/data.tf`, y su comentario dice por qué: *resolver por `data` blocks hace que un rename falle en `plan` y no en `apply`*. Se aplica igual acá:

- El proyecto de Longevo resuelve el topic con `data "aws_sns_topic"` por nombre.
- El proyecto de Open Wearables resuelve la cola con `data "aws_sqs_queue"` por nombre, sólo si necesita el ARN.

Los nombres son deterministas (`<environment>-<project>-<name>`, más `.fifo`), así que no hace falta acoplar los estados ni ordenar los applies más allá de "el topic antes que la suscripción".

### Lo que ya está resuelto y no hay que construir

- **`src/modules/sqs` ya hace lo que necesitamos.** Soporta `fifo_queue`, y crea la **DLQ con su redrive policy** sin trabajo extra. Alcanza con instanciarlo con `fifo_queue = true`.
- **Atención con `content_based_deduplication`.** El módulo lo pone en `true` automáticamente cuando la cola es FIFO. Es compatible con el diseño —un `MessageDeduplicationId` explícito tiene precedencia sobre el hash del cuerpo— y como el evento lleva `batch_id` y `occurred_at`, dos eventos legítimos nunca comparten cuerpo. Conviene saberlo igual: es una segunda red de deduplicación que nadie declaró explícitamente.
- **El NAT Gateway ya existe** (`open-wearables/environments/qa/network.tf`). Eso hace que **el VPC endpoint de interface para SNS pase de obligatorio a opcional**: sin NAT era la única salida; con NAT es una optimización de costo y latencia. Se decide por número, no por diseño.

### Lo que falta y no es de este diseño

`src/open-wearables/environments/` **sólo tiene `qa`**. El puente se construye ahí primero, y `prod` entra cuando exista el ambiente, siguiendo el orden del diseño de infra de agosto (un módulo por PR, qa antes que prod).

Nota sobre el layout: el proyecto real divergió de lo que proponía la sección 11 del diseño de agosto —los recursos viven planos en `environments/qa/*.tf`, y `modules/` sólo tiene `constants` y `service`—. Los archivos nuevos siguen la convención real del repo, no la propuesta original.

## 11. Observabilidad

Sobre la base ya definida en la sección 9 del diseño de infra, agregar:

| Señal | Por qué |
|---|---|
| Eventos `sync.completed` publicados por minuto | Es el latido de la integración. Cero por 30 minutos significa que la ingesta o la emisión se cortaron |
| Antigüedad del mensaje más viejo en la cola | Detecta consumidor trabado antes de que la profundidad se dispare |
| Mensajes en la DLQ mayor a cero | Eventos que se perderían sin aviso |
| Latencia evento → pull completado, p50 y p99 | Es el número que el requisito de near-real-time promete. Sin esto no hay forma de saber si se cumple |
| Ratio de pulls vacíos | Detecta la ventana de visibilidad de la sección 7 si existe |
| **Eventos descartados por falta de ventana > 0** | Un camino de ingesta habilitado cuyo `metadata` no propaga la ventana deja de llegar al dominio **en silencio**. Es la contracara de descartar en vez de puleear a ciegas, y por eso es alarma y no sólo log |

## 12. Testing

- **Contrato del evento.** Un fixture del JSON de `sync.completed` versionado en los dos repos: el fork asegura que lo emite así, el monorepo que lo parsea así. Es lo único que evita que un cambio en el productor rompa al consumidor en silencio.
- **Replay.** El mismo evento dos veces produce cero documentos nuevos.
- **La noche fragmentada.** Simular los payloads consecutivos de una noche de Apple y afirmar que Longevo termina con **un** documento de sueño. Cubre el hallazgo de la sección 7 y tiene que correr contra datos de sueño reales, no contra un mock.
- **Shadow run en QA.** Una cohorte piloto con Spike y Open Wearables alimentando en paralelo, comparando los resúmenes diarios de las dos fuentes. Es el criterio de aceptación real antes de habilitar producción.

## 13. Ítems de verificación

Bloqueantes:

| # | Ítem | Bloquea |
|---|---|---|
| V1 | Prueba de reprocesamiento real sobre datos de sueño, validando el snapshot-replace | Primera cohorte |
| V2 | Confirmar la visibilidad transaccional en el punto de emisión: el manejo de sueño ocurre después del commit de `import_data_from_request` | Habilitar la emisión |
| V0 | ~~Inventario de providers habilitados~~ **Resuelto:** Whoop, Strava, Oura, Apple Health, Health Connect y Samsung Health; Garmin diferido por credenciales. Superficie agrupada en la sección 4.1 | — |
| V3 | Confirmar que la cola FIFO sostiene el volumen coalescido con `MessageGroupId` por usuario, y si hace falta modo de alto throughput | Diseño de la cola |
| V4b | Confirmar que el evento terminal de un **borrado** propaga la ventana del registro eliminado, en Whoop y Strava. Sin eso, el borrado no se replica en Longevo y no hay señal de que faltó | Habilitar Whoop y Strava |
| V4 | Deduplicación o coalescencia para Garmin, que emite **cinco** eventos terminales con `run_id` propio por push (sección 4.1). No bloquea fase 1 porque Garmin está diferido, pero sí bloquea habilitarlo | Habilitar Garmin |

No bloqueantes:

| # | Ítem |
|---|---|
| V5 | Cuántos eventos `sync.completed` por día produce el volumen real, para dimensionar la cola |
| V6 | Cómo factura Spike el MAU exactamente, para calibrar el momento de la baja de conexiones |
| V7 | Si el ítem V9 del diseño de infra se resuelve por cuenta separada, definir la política cross-account |

## 14. Fase 2

- **Store contra cache.** Reemplazar el store durable de `wearables-v2` por un cache con TTL invalidado por el propio evento `sync.completed`, que ya trae usuario y ventana. Elimina una segunda copia durable de PHI y su superficie de compliance. Requiere resolver `getSpikeWorkoutById`, que hoy no tiene equivalente por ID en la External API: un endpoint `GET /api/v1/users/{user_id}/events/workouts/{record_id}` en el fork, contribuible upstream.
- **Flag `read_only` en `ApiKey`** (sección 6), cuando haya un segundo consumidor.
- **Providers OAuth que falten habilitar.** No hay productor nuevo que escribir: alcanza con enriquecer el `metadata` de su handler con la ventana, y el evento fluye por el mismo camino.
- **Svix**, si aparece un consumidor externo a la cuenta.

## 15. Referencias al código

| Tema | Ubicación |
|---|---|
| Punto de emisión propuesto | `backend/app/services/sync_status_service.py` (`emit_event`, `completed`, `webhook_delivered`) |
| Caminos de ingesta | `backend/app/integrations/celery/tasks/{process_sdk_upload,webhook_push,sync_vendor_data,process_xml_upload}_task.py` |
| Merge y recreación de sueño | `backend/app/services/apple/healthkit/sleep_service.py:425` |
| Eventos salientes actuales (Svix) | `backend/app/services/outgoing_webhooks/` |
| External API consumida | `backend/app/api/routes/v1/{summaries,events,timeseries}.py` |
| Autenticación por API key | `backend/app/services/api_key_service.py` |
| Modelo de usuario y `external_user_id` | `backend/app/models/user.py` |
| Flujo actual de Spike en Longevo | `monorepo-backend/apps/wearables-api/src/services/spike-webhook-{sqs.service,queue.handler}.ts` |
| Use cases a replicar | `monorepo-backend/apps/wearables-api/src/use-cases/` |
| SDK de Open Wearables ya construido | `monorepo-backend/libs/openwearables-sdk/` |
| Resolución de fuente default | `monorepo-backend/libs/wearables-sdk/src/infraestructure/default-wearable-source.service.ts` |
| Sesión y `openWearablesUserId` | `monorepo-backend/apps/wearables-api/src/services/openwearables-api.service.ts` |
