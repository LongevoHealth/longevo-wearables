# Handoff — fase 1 de la ingesta Open Wearables → Longevo

**Fecha:** 2026-09-12
**Estado:** implementada y revisada. El review final de rama dictaminó *safe to merge*. **Nada aplicado en AWS.**

Acompaña a [el diseño](./2026-09-11-ingesta-open-wearables-longevo-design.md) y [el plan](../plans/2026-09-11-ingesta-open-wearables-fase1.md).

## Ramas

| Repo | Rama | Rango |
|---|---|---|
| `longevo-wearables` | `feat/ow-sync-events` | `9bd9215..93328ce` |
| `longevoIac` | `feat/ow-sync-events` | `d2f2308..1cb9626` |
| `monorepo-backend` | `feat/openwearables-integration` | `758a04ad3..d44ca58` |

Suites al cierre: fork 2065 passed / 2 skipped · `wearables-api` 242 · `wearables-v2-sdk` 63. `tsc --noEmit` y `nx build wearables-api` limpios; `terraform validate` y `fmt` limpios.

## Antes de aplicar en QA

1. **Aplicar la infraestructura** en este orden: primero el topic (`src/open-wearables/environments/qa/sync-events.tf`), después la cola y la suscripción (`src/longevo/wearables/environments/qa/open-wearables-sync-events.tf`), que resuelve el topic por `data` y falla en `plan` si el primero no está. Por último la regla de WAF.
2. **Verificar la regla de WAF con `curl` contra el host real**, los siete casos del reporte de la Task 6: los tres DELETE destructivos deben dar 403; `DELETE .../connections/{provider}`, `POST /users`, `PATCH /users/{id}` y `POST /users/{id}/token` deben seguir funcionando. Esos cuatro últimos son la integración: si se bloquean, `createSession` deja de andar.
3. **Cablear el lado consumidor, que no vive en `longevoIac`.** Ninguno de los tres repos falla si esto falta, y sin esto la cola existe y nadie la lee:
   - `sqs:ReceiveMessage`, `DeleteMessage` y `GetQueueAttributes` sobre la cola, en el task role de `wearables-api`
   - `OPENWEARABLES_SYNC_QUEUE_NAME` y `OPENWEARABLES_SYNC_QUEUE_URL`
   - el secreto con la API key de lectura de Open Wearables
   - el metric filter sobre el log group de `wearables-api` que matchea el literal `openwearables.sync_event.discarded`
4. **Decidir el destino de las alarmas.** La alarma de DLQ quedó sin `alarm_actions` porque el proyecto no tiene convención de topic de alertas. Tal como está se ve en consola y no notifica a nadie — y es justamente la que detecta que un camino de ingesta se apagó.
5. **Rebase antes del merge**: la rama del monorepo está tres migraciones detrás de `main`.

## Gate antes de la primera cohorte

- **La noche fragmentada, contra dato real.** Sincronizar una noche completa en varios payloads y confirmar en DocumentDB que queda **un** documento de sueño. Es el ítem V1 del diseño y el bug que el review final encontró vivía exactamente ahí.
- **La noche en vivo.** Subir un payload de sueño dentro de `sleep_end_gap_minutes` del último sample y confirmar que el documento aparece. Verifica el arreglo del hallazgo 2.
- **Un usuario Sura no debe migrarse.** Confirmar que el log de rechazo aparece y que su fuente default sigue siendo Spike.

## Follow-ups conocidos

**Bloquean la primera cohorte:**

- **`getWorkout()` en Nicoya rompe para workouts de Open Wearables.** `WearablesAdminV3Service.getWorkout()` lee el registro persistido y re-consulta la API viva de Spike con `workoutId`, que para OW es un UUID que Spike nunca vio → 404. Es **permanente**, no sólo durante la migración. Arreglo mínimo: ramificar por procedencia y devolver el documento persistido cuando la fuente es OW.
- **`score` vs `efficiency_percent`.** El writer mapea el `efficiency_percent` de OW al campo `score`, que en Spike es un score de sueño. Los dos van de 0 a 100, así que nada falla — y el shadow run compara dos magnitudes distintas y lo reporta como deriva.
- **Sura.** La integración de OW es ciega a Sura de punta a punta. Ahora hay un gate que impide la pérdida de datos, pero habilitar Sura es un proyecto propio: propagar la plataforma desde la sesión hasta el evento.

**No bloquean:**

- `modules/sqs` no expone output de la DLQ, así que la dimensión de la alarma es un string reconstruido que se pudre en silencio si cambia la convención del módulo.
- La regla preexistente `sdk-ingest-rate-limit` usa `text_transformation = NONE`, el mismo patrón que era esquivable en la regla nueva.
- `publish_sync_notification_task.py` no tiene guarda ni reintento en la búsqueda en base: un corte de base pierde la notificación **sin reintentar**, que es lo transitorio que sí debería reintentar.
- `.gitignore` del fork tiene un `*.json` general: cualquier fixture JSON futuro se cae del `git add` sin error.
- Los métodos nuevos del repositorio están sólo en la clase concreta, no en la interfaz `WearablesRepository`.
- El filtro del upsert de sueño es `{ sleep_id }` sin calificar por scope. Inalcanzable en la práctica, pero si dos scopes colisionaran reasignaría en silencio un registro de salud a otro usuario.
- La cola no está cifrada con la CMK del ambiente, aunque el topic sí. Mismo metadato, dos niveles de protección.
- Un `queueUrl` vacío deja el consumidor registrado fallando en loop en vez de frenar el arranque. Aplica a las tres colas del servicio.
- Sin verificar: si `URL_DECODE` de AWS trata `+` como espacio donde Starlette no. El review final concluyó que no afecta la corrección de la regla, pero la inferencia no pudo confirmarse contra documentación.

## Fase 2

Sin cambios respecto de la sección 14 del diseño: enriquecer el `metadata` de Whoop, Strava y Oura; deduplicación para Garmin; cohortes y baja diferida de Spike; y el shadow run.
