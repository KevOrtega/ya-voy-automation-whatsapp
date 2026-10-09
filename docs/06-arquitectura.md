# 06 — Arquitectura

## 1. Contexto (C4 nivel 1)

```mermaid
flowchart TB
    store(["👤 Local<br/>Envía, confirma y cancela pedidos por WhatsApp"])
    courier(["👤 Motero<br/>Toma pedidos reaccionando en el grupo"])
    admin(["👤 Administrador<br/>Recibe alertas, el reporte diario y sus correcciones,<br/>cancela o libera pedidos y amplía o cierra el horario"])

    yavoy["<b>Ya Voy</b><br/>Sistema de despacho de pedidos por WhatsApp"]

    wa[("WhatsApp<br/>(red de Meta)")]
    llm["Proveedor de LLM<br/>Extracción estructurada"]

    store -- "mensajes privados" --> wa
    courier -- "reacciones en el grupo" --> wa
    admin -- "comandos y cambios de horario<br/>por privado, reacciones en el grupo" --> wa
    wa <-- "eventos y envíos<br/>(protocolo de WhatsApp Web)" --> yavoy
    yavoy -- "HTTPS: texto a OrderExtraction<br/>o ScheduleOverrideIntent" --> llm
    wa -- "alertas, reporte .xlsx y correcciones" --> admin
```

Todos los actores interactúan solo por WhatsApp; no hay interfaz web para usuarios.

## 2. Contenedores (C4 nivel 2)

```mermaid
flowchart TB
    wa[("WhatsApp")]
    llm["Proveedor de LLM"]

    subgraph sys["Ya Voy"]
        evo["<b>evolution</b><br/>Evolution API v2 (Node, Baileys)<br/>Sesión de WhatsApp del número del bot"]
        web["<b>web</b><br/>FastAPI<br/>Webhook y /health"]
        worker["<b>worker</b><br/>arq<br/>Lógica de negocio, envíos,<br/>tareas programadas, reporte xlsx"]
        pg[("<b>postgres</b><br/>PostgreSQL 16<br/>DB yavoy + DB evolution")]
        redis[("<b>redis</b><br/>Redis 7<br/>Cola arq, locks, límites, caché de Evolution")]
    end

    wa <--> evo
    evo -- "POST /webhooks/evolution/{secret}" --> web
    web -- "INSERT inbound_events" --> pg
    web -- "enqueue" --> redis
    redis -- "jobs" --> worker
    worker -- "SQL" --> pg
    worker -- "REST: sendText, sendMedia,<br/>deleteMessageForEveryone, updateMessage" --> evo
    worker -- "HTTPS" --> llm
    evo -- "SQL" --> pg
    evo -- "caché" --> redis
```

### Responsabilidades

| Contenedor | Tecnología | Responsabilidad | No hace |
|------------|-----------|-----------------|---------|
| `web` | FastAPI + Uvicorn, SQLAlchemy 2 (async) + asyncpg | Validar el secreto del webhook, descartar lo obviamente irrelevante (ver reglas de descarte abajo), insertar en `inbound_events` sin los campos secretos del sobre (`destination`, `apikey`), encolar, responder rápido. `/health`. | Llamar al LLM o a Evolution. No expone API de administración. |
| `worker` | arq, openpyxl | `process_inbound_event`, `evaluate_draft`, `send_outbound`; tareas cron: vencimiento de borradores y confirmaciones de pedido, alerta de pedidos sin tomar, vencimiento de confirmaciones de horario, recordatorio de cierre vigente, reporte diario y correcciones (con espera de pendientes), retención y barridos de recuperación. | Exponer HTTP. |
| `postgres` | PostgreSQL 16 | Fuente de verdad. Dos bases: `yavoy` (aplicación) y `evolution` (interna de Evolution). | — |
| `redis` | Redis 7 | Cola de arq, lock por local, lock del límite de envío, contadores de límite por remitente, caché de Evolution (con prefijo propio). | Guardar estado de negocio. |
| `evolution` | Evolution API v2 | Mantener la sesión de WhatsApp (QR), entregar eventos por webhook, enviar textos y documentos, eliminar y editar mensajes propios. | Lógica de negocio. |

### Tareas programadas (arq)

Todas corren en el `worker` como una única tarea cron cada `SCHEDULER_INTERVAL_SECONDS` (o una por
tarea con el mismo intervalo). Son idempotentes y "recuperables": comparan contra la hora actual en
lugar de depender de ejecutarse en un minuto exacto, así que si el worker estuvo caído hacen lo pendiente al volver.

| Tarea | Condición | Efecto |
|-------|-----------|--------|
| `expire_drafts` | `draft` o `awaiting_confirmation` con `draft_expires_at < now()` | T09 |
| `alert_unclaimed_orders` | `published`, sin alerta, `unclaimed_alert_at < now()` | T15 |
| `expire_schedule_confirmations` | `schedule_overrides` en `pending_confirmation` con `confirmation_expires_at < now()` | Pasan a `expired` |
| `remind_still_closed` | Hora local ≥ `BUSINESS_HOURS_START`, hay un `closed_until_further_notice` activo y no se avisó hoy | `admin_schedule_still_closed` |
| `process_daily_reports` | `now() ≥ report_due_at(D)` para el día operativo D sin reporte, o reportes en `waiting`/`failed` | CU-16: espera pendientes, recordatorio a los `REPORT_PENDING_REMINDER_MINUTES` del cierre efectivo (una vez), genera y envía la revisión 1 |
| `process_report_corrections` | `daily_reports` en `correction_pending` con `due_at <= now()`; si hay pendientes, espera (y si fue reabierto por una apertura extra, recordatorio a los `REPORT_PENDING_REMINDER_MINUTES`) | CU-17: sin pendientes, compara con la última revisión enviada; si hay cambios netos, genera y envía la corrección; si no, vuelve a `sent` |
| `recover_inbound_events` | `inbound_events` en `received` con más de `INBOUND_RECOVERY_SECONDS` | Reencolar |
| `recover_outbound_messages` | `outbound_messages` en `pending` con más de `OUTBOUND_RECOVERY_SECONDS` | Reencolar |
| `apply_data_retention` | Una vez por día | Anonimizar lo más viejo que `DATA_RETENTION_DAYS` |

### Componentes internos (paquete `ya_voy`, propuesta)

```
ya_voy/
  api/            # FastAPI app, routers: webhooks, health
  worker/         # arq WorkerSettings, job functions, scheduled tasks
  domain/         # order state machine, validation, command parsing (pure code, no I/O)
  schedule/       # base hours + schedule_overrides, business_date, is_accepting_orders, report_due_at, override validation and confirmation flow
  identity/       # sender classification, store/courier auto-registration, lid/phone resolution and merge
  llm/            # provider-agnostic client; OrderExtractor and ScheduleIntentParser with their schemas and prompts
  whatsapp/       # Evolution API client, webhook payload parsing (jid/lid helpers)
  messaging/      # templates catalog, outbox enqueue/render
  reports/        # daily report queries, snapshots/diff for corrections, xlsx builder (openpyxl)
  db/             # SQLAlchemy models, repositories, Alembic migrations
  config.py       # pydantic-settings
```

- **`domain`** no depende de FastAPI, arq ni Evolution: recibe eventos normalizados
  (`StoreTextReceived`, `StoreConfirmationReceived`, `StoreCancelRequested`, `ReactionAdded`, `ReactionRemoved`,
  `CourierTextReceived`, `AdminCommandReceived`, `AdminScheduleTextReceived`, `AdminConfirmationReceived`, ...) y decide transiciones.
  Es lo que más se testea.
- **`schedule`** concentra el horario: `business_date(now)`, `is_accepting_orders(now)` (horario base más aperturas
  extra menos cierres activos, con el motivo del cierre), `report_due_at(D)` (cierre efectivo del día), la validación de `ScheduleOverrideIntent`, el cálculo de `starts_at`/`ends_at` y el ciclo de
  confirmación. Es código puro salvo la lectura de `schedule_overrides`.
- **`identity`** clasifica al remitente (admin, motero, local) y hace el alta automática. Concentra todo
  lo que depende de cómo WhatsApp expone `@lid` y números, que es lo más cambiante.
- **`llm`** define dos interfaces: `OrderExtractor.extract(text, current) -> OrderExtraction` y
  `ScheduleIntentParser.parse(text, now, base_hours) -> ScheduleOverrideIntent`. La implementación concreta usa
  un cliente compatible con varios proveedores (por ejemplo `pydantic-ai`, `instructor` o `litellm`; se elige al
  implementar; la etapa 4a usa `httpx` directo contra la API de Chat Completions compatible con OpenAI,
  `ya_voy/llm/openai_compatible.py`). Proveedor y modelo por configuración (RNF-13). El LLM nunca decide: su salida se valida con
  código y, en el caso del horario, además la confirma el administrador.
- **`whatsapp`** encapsula el formato de Evolution: normaliza números, JID y `@lid` (incluidos los campos
  alternativos), detecta reacciones (`message.reactionMessage`, con `text` vacío = remoción) y mensajes
  citados (`contextInfo.stanzaId`).
- **`reports`** arma las filas con SQL, guarda la foto (`snapshot`) de cada revisión, calcula los cambios
  netos contra la última revisión enviada (`diff_snapshots`) y genera el `.xlsx` en memoria con openpyxl
  (encabezados en negrita, columnas de fecha con formato `dd/mm/yyyy hh:mm`, filtros automáticos; hoja
  `Correcciones` en las correcciones).

#### Reglas de descarte del webhook

El webhook responde `200` sin guardar nada (`{"status": "ignored", "reason": ...}`) cuando el evento:

- no es `messages.upsert` ni `connection.update` (`unsupported_event`);
- viene de otra instancia de Evolution (`other_instance`);
- es `fromMe` (`from_me`), de `status@broadcast` u otra difusión (`status_broadcast`) o no tiene chat (`no_chat`);
- es de un grupo distinto de `COURIERS_GROUP_JID` (`other_group`);
- es del grupo de moteros pero no es una reacción (`group_not_reaction`): la charla del grupo no se guarda
  (RF-02, RN-12, privacidad). Si una reacción apunta a la publicación de un pedido lo decide el worker,
  porque necesita la base.

Códigos de respuesta (Evolution reintenta todo salvo 400/401/403/404/422): `404` secreto incorrecto, `400`
payload que no es JSON o no es un evento de Evolution (sin bucle de reintentos), `503` base no disponible
(Evolution reintenta; la deduplicación lo hace seguro), `200` en los demás casos. Si falla el encolado
después de guardar, igual responde `200` y el barrido `recover_inbound_events` lo recupera.

#### Eventos entrantes fallidos

Si `process_inbound_event` falla, el evento queda en `inbound_events.status = 'failed'` con `error` y se
registra en el log a nivel `ERROR` con `inbound_event_id` y el error. No se reintenta solo (el barrido
toma solo `received`). Para reprocesarlos, después de corregir la causa:

```bash
make reprocess ids="12 15"   # esos eventos, si siguen en failed
make reprocess ids=--all     # todos los eventos en failed
```

(`python -m scripts.reprocess_inbound` dentro del `worker`). Pasa a `received` solo los que siguen en
`failed` (un `UPDATE` condicional, así que repetirlo no hace nada), borra `error` y `processed_at`, conserva
`attempts` como historial y los encola de nuevo.

#### Toma de pedidos (etapa 4a)

`ya_voy/worker/intake.py` recibe el texto de un local ya identificado (`StoreText`) y hace T01, T02 y T05.
La llamada al LLM nunca se hace con una conexión a la base tomada ni dentro de una transacción:

1. Toma el lock de Redis del local (`lock:store:{store_id}`, `SET NX PX` con un token aleatorio). Si está
   tomado, espera (sin conexión a la base) hasta un TTL. Así los mensajes de un mismo local se procesan de a
   uno y en orden; los de locales distintos, en paralelo.
2. Transacción corta: lee el borrador abierto y su revisión (`last_store_message_at`), renueva
   `draft_expires_at` (para que `expire_drafts` no lo cierre mientras corre el LLM) y hace commit.
3. Renueva el TTL del lock y llama a `OrderExtractor` sin conexión a la base.
4. Transacción corta: bloquea el borrador (`SELECT ... FOR UPDATE`) y verifica que la revisión no cambió. Une
   los campos (lo nuevo no vacío reemplaza; `payment_method` lo decide el diccionario de
   `ya_voy/domain/payment.py` cuando hay texto de pago), renueva `draft_expires_at = now() + DRAFT_TTL_MINUTES`
   y guarda el mensaje. Si el local no tenía borrador, lo crea con `INSERT ... ON CONFLICT DO NOTHING` sobre
   `orders_one_draft_per_store` (RN-09).
5. Libera el lock solo si el token sigue siendo el propio (script Lua: comparar y borrar).

La verificación de revisión es un respaldo, no el camino normal: con el lock tomado nadie más escribe
`last_store_message_at`. Solo si el lock se perdiera (por ejemplo, Redis reiniciado) la revisión cambia o el
`INSERT` encuentra otro borrador; entonces vuelve al paso 2 con el borrador más reciente, hasta
`MAX_APPLY_ATTEMPTS = 3` veces, y si sigue cambiando el evento falla (queda para `make reprocess`).

**TTL del lock.** Se calcula con la configuración, no es una constante, y siempre es estrictamente mayor que
el peor caso del LLM:

```
peor_caso = LLM_TIMEOUT_SECONDS × (LLM_MAX_RETRIES + 1) + 0,5 s × (2^LLM_MAX_RETRIES − 1)
TTL       = peor_caso + max(15 s, 25 % de peor_caso)
```

El segundo término del peor caso es la suma de las esperas entre reintentos (0,5 s, 1 s, 2 s, ...). Con los
valores por defecto (20 s, 3 reintentos): peor caso 83,5 s y TTL ≈ 104,4 s. El margen cubre las dos
transacciones cortas y la latencia de Redis. Además, el TTL se renueva antes de cada llamada al LLM. Si el
worker se cae, el TTL libera al local.

Los comandos (`sí`, `no`, `cancelar`, `ayuda`) también toman el lock pero no pasan por el LLM. Si el LLM falla,
el borrador queda igual y se envían `store_processing_error` y `admin_llm_failure`.

Después del commit se encola `evaluate_draft(order_id)` diferido `DRAFT_DEBOUNCE_SECONDS` (RF-15). Si llegó otro
mensaje en ese lapso, la evaluación se descarta (la del mensaje nuevo evalúa después). Con campos faltantes envía
`store_order_missing_fields` (y `store_order_format_example` desde el pedido número `MAX_MISSING_FIELDS_PROMPTS`);
completo, pasa a `awaiting_confirmation` (T04) y envía `store_order_confirm_request`. Las claves de la bandeja de
salida incluyen la revisión del borrador (`last_store_message_at`), así que evaluar dos veces lo mismo no
duplica mensajes. `expire_drafts` (T09) es un `UPDATE` condicional sobre `draft_expires_at`: un borrador cuyo mensaje se está
procesando ya fue renovado en el paso 2 y no vence.

Pendiente para la etapa 4b (`TODO(stage-4b)`): clasificar al remitente y dar de alta locales (`identity`,
`@lid`), conectar la ruta `private_text` con `drafts.handle_store_text` (fuera de la transacción que bloquea el
evento entrante, marcándolo procesado con `finalize` en la transacción del paso 4), los efectos de los comandos (T06-T08),
`store_help` con su espera, `store_unsupported_media` y el control de horario `is_accepting_orders` (RN-18).

#### Política de reintentos de la bandeja de salida

`send_outbound` distingue errores transitorios y permanentes de Evolution:

- **Transitorios** (error de red o timeout, `5xx`, `408`, `429`): reintento con backoff exponencial
  (2, 4, 8, ... segundos, tope 5 minutos) hasta `OUTBOUND_MAX_ATTEMPTS`.
- **Permanentes** (cualquier otro `4xx`, respuesta `2xx` inutilizable, archivo del reporte faltante): pasa
  directo a `failed`, sin reintentos.

Al quedar en `failed` (por cualquiera de las dos vías) se encola `admin_outbound_failed` para cada
administrador, salvo que el mensaje fallido sea esa misma alerta. El backoff vigente se guarda en Redis
(`outbound:backoff:{id}`) para que el barrido no lo saltee.

### Decisiones de diseño

| # | Decisión | Motivo |
|---|----------|--------|
| D-01 | Webhook que solo persiste y encola; todo el procesamiento en el worker | Responder rápido a Evolution, tolerar picos y reintentar sin perder eventos. |
| D-02 | Asignación por `UPDATE` condicional en Postgres | Única garantía real contra la doble reacción; no depende de Redis ni del orden de llegada. |
| D-03 | Bandeja de salida (`outbound_messages`) con clave de idempotencia, también para documentos | Reintentos sin mensajes duplicados; registro de lo enviado; límite de envío centralizado. |
| D-04 | Lock por local en Redis + `SELECT ... FOR UPDATE` del borrador | Serializar ráfagas de mensajes del mismo local sin bloquear a los demás. |
| D-05 | LLM solo para extracción; validación, comandos y horario en código | Respuestas predecibles, testeables y sin costo de LLM para comandos. |
| D-06 | Postgres compartido (dos bases) en desarrollo; servicios separados en producción | Simplicidad local; aislamiento en producción. |
| D-07 | Identidad por id de WhatsApp (JID o `@lid`), con número opcional | WhatsApp oculta cada vez más el número en grupos; el alta automática no puede depender de él. |
| D-08 | Administración solo por WhatsApp (comandos y lenguaje natural) | El administrador ya vive en WhatsApp; evita construir y asegurar un panel en v1. |
| D-09 | Tareas programadas por comparación con la hora actual y registros de idempotencia | Funcionan aunque el worker se reinicie; el reporte no se duplica ni se pierde. |
| D-10 | Cambios de horario: LLM interpreta, código valida, administrador confirma | Comodidad del lenguaje natural sin aplicar nunca una mala interpretación sin aprobación. |
| D-11 | Sin seguimiento de entrega; el pedido se cuenta al tomarse | Decisión del cliente; simplifica estados, mensajes y reporte. |
| D-12 | El reporte espera a que no haya pedidos pendientes de la fecha | Los conteos del reporte son definitivos al enviarse. |
| D-13 | Confirmación explícita del local antes de publicar | Evita publicar interpretaciones erróneas del LLM; el local ve exactamente lo que verán los moteros. |
| D-14 | Al cancelar se elimina la publicación (y las respuestas del bot) del grupo; si falla, se edita como CANCELADO | Los moteros no ven pedidos muertos; no se depende de que la eliminación funcione siempre. |
| D-15 | Cancelar contra tomar se resuelve con `SELECT ... FOR UPDATE` + `UPDATE` condicional sobre el estado leído | El motero recibe aviso de cancelación si y solo si llegó a tener el pedido. |
| D-16 | Día operativo (`business_date`) con corte a `BUSINESS_DAY_ROLLOVER_TIME` | Las aperturas después de medianoche siguen perteneciendo al día que las originó, también en el reporte. |
| D-17 | Reporte versionado (`report_revisions`) con correcciones por diferencia neta | Los cambios posteriores al envío se informan sin reescribir lo enviado ni mandar correcciones vacías. |

## 3. Entorno de desarrollo (Docker Compose)

Servicios previstos en `docker-compose.yml` (se crean al armar el entorno, no en esta etapa):

| Servicio | Imagen / build | Puertos (host) | Volúmenes | Depende de |
|----------|----------------|----------------|-----------|------------|
| `postgres` | `postgres:16-alpine` | `5432` | `pg_data` | — |
| `redis` | `redis:7-alpine` | `6379` | `redis_data` | — |
| `evolution` | `evoapicloud/evolution-api:<versión fija>` | `8080` (manager y QR) | `evolution_instances` | `postgres`, `redis` |
| `web` | `build: .` — `uvicorn ya_voy.api.main:app --reload` | `8000` | código montado | `postgres`, `redis` |
| `worker` | `build: .` — `arq ya_voy.worker.WorkerSettings --watch ya_voy` | — | código montado | `postgres`, `redis` |
| `migrate` | `build: .` — `alembic upgrade head` (una sola ejecución) | — | — | `postgres` |

Notas:

- Un script de inicialización de Postgres crea las bases `yavoy` y `evolution`.
- Evolution llama al webhook por la red interna de Compose: `http://web:8000/webhooks/evolution/<secret>`.
- La instancia de WhatsApp se crea y vincula escaneando el QR desde el manager de Evolution (`http://localhost:8080/manager`) con un número de pruebas, **nunca** el número de producción del cliente. Se arma un grupo de prueba que hace de grupo de moteros.
- Para probar sin teléfono se prevén fixtures con payloads reales de Evolution (texto, reacción, remoción de reacción, cita, participante `@lid` con y sin número alternativo, documento enviado) y un comando para reenviarlos al webhook.
- Para probar horario, cierres, aperturas y reporte se puede fijar la hora con una variable de desarrollo (`FAKE_NOW`) o con `BUSINESS_HOURS_*` y `REPORT_PENDING_REMINDER_MINUTES` cercanos a la hora actual.
- La eliminación y edición de mensajes del grupo se prueban en el grupo de prueba, incluido el caso de un mensaje viejo que WhatsApp ya no deja borrar.
- La interpretación de horario tiene su propio conjunto de frases de prueba (los ejemplos del [modelo de datos](05-modelo-datos.md) y variantes).
- Se usa el mismo `Dockerfile` para `web`, `worker` y `migrate`, que es el que Dokku construirá en producción.

## 4. Producción en Dokku (mapeo)

| Desarrollo (Compose) | Producción (Dokku) | Comandos de referencia |
|----------------------|--------------------|------------------------|
| `postgres` (base `yavoy`) | Plugin `dokku-postgres`, servicio `yavoy-db`, vinculado a la app `yavoy` | `dokku postgres:create yavoy-db` · `dokku postgres:link yavoy-db yavoy` (inyecta `DATABASE_URL`) |
| `postgres` (base `evolution`) | Servicio separado `evolution-db` vinculado a la app `evolution` | `dokku postgres:create evolution-db` · `dokku postgres:link evolution-db evolution` |
| `redis` | Plugin `dokku-redis`, servicio `yavoy-redis`, vinculado a ambas apps (Evolution con `CACHE_REDIS_PREFIX_KEY` propio) | `dokku redis:create yavoy-redis` · `dokku redis:link yavoy-redis yavoy` (inyecta `REDIS_URL`) |
| `web` | App `yavoy`, proceso `web` del `Procfile` | `dokku ps:scale yavoy web=1 worker=1` |
| `worker` | App `yavoy`, proceso `worker` del `Procfile` | — |
| `migrate` | `app.json` → `scripts.dokku.predeploy` | — |
| `evolution` | App `evolution` desplegada desde imagen Docker, con almacenamiento persistente | `dokku git:from-image evolution evoapicloud/evolution-api:<versión>` · `dokku storage:mount evolution /var/lib/dokku/data/storage/evolution:/evolution/instances` |

`Procfile` previsto:

```
web: uvicorn ya_voy.api.main:app --host 0.0.0.0 --port ${PORT:-5000} --proxy-headers
worker: arq ya_voy.worker.WorkerSettings
```

`app.json` previsto:

```json
{
  "scripts": { "dokku": { "predeploy": "alembic upgrade head" } },
  "healthchecks": { "web": [{ "type": "startup", "name": "web", "path": "/health", "attempts": 5 }] }
}
```

Consideraciones:

- **Red interna:** Evolution y `yavoy` se conectan a una red de Dokku compartida (`dokku network:create yavoy-net` y `network:set ... attach-post-create`), así el webhook va a `http://yavoy.web:5000/...` y el worker llama a Evolution sin salir a internet. El manager de Evolution se expone con dominio propio, HTTPS (`dokku-letsencrypt`) y `AUTHENTICATION_API_KEY`.
- **`DATABASE_URL`:** Dokku la inyecta como `postgres://...`; la configuración la convierte a `postgresql+asyncpg://...`.
- **Zona horaria:** `TZ` se configura en la app (`dokku config:set yavoy TZ=America/Argentina/Buenos_Aires`); la aplicación igual calcula horario, cierres y fecha del reporte con `zoneinfo` a partir de `TZ`, sin depender del reloj del VPS.
- **Escalado:** un solo proceso `worker` alcanza al inicio; las garantías de corrección están en Postgres, así que se puede escalar sin cambios.
- **Backups:** `dokku postgres:backup-schedule` para ambas bases. La carpeta de instancias de Evolution contiene la sesión de WhatsApp: perderla obliga a escanear el QR de nuevo.

## 5. Riesgos

| # | Riesgo | Mitigación |
|---|--------|------------|
| R-01 | Bloqueo del número (Baileys no es oficial). | Número dedicado y "calentado", límite de envío, sin difusión masiva, sin iniciar conversaciones con desconocidos. |
| R-02 | Sin registro previo, cualquiera que conozca el número puede cargar pedidos que llegan al grupo. | Límite por remitente, bloqueo manual (`is_blocked`), nombre del local visible en el grupo y en el reporte, alerta al admin ante abuso. |
| R-03 | Moteros conocidos solo por `@lid`: el reporte no tiene su teléfono y el envío de privados a un `@lid` depende de la versión de Baileys/Evolution. | Guardar el número cuando el evento lo trae (`participantAlt`, `senderPn`, etc.); verificar con eventos reales el envío a `@lid`; si un privado falla, `admin_outbound_failed` y el grupo igual muestra "Asignado a". |
| R-04 | La misma persona vista una vez por `@lid` y otra por número genera dos moteros. | Fusión automática cuando un evento trae ambos identificadores (ver [modelo de datos](05-modelo-datos.md)). |
| R-05 | Un motero que nunca reaccionó en el grupo escribe por privado y se lo trata como local. | Si manda un pedido, se publica como de un local más; el nombre visible lo delata. Se corrige solo cuando reacciona por primera vez (pasa a ser motero conocido). |
| R-06 | El administrador escribe desde un chat que WhatsApp expone solo como `@lid` y no se lo reconoce como admin: sus comandos y cambios de horario se tratarían como pedidos de un local. | Comparar también contra el número alternativo del evento; verificar con su número real al armar el entorno; agregar `ADMIN_JIDS` si hiciera falta. |
| R-07 | Pedidos publicados cerca del cierre quedan sin tomar después del cierre efectivo y retienen el reporte. | Alerta de pedido sin tomar a los 20 minutos y recordatorio a las 2 h del cierre efectivo (01:00 en un día normal) con los códigos; el administrador puede cancelarlos o tomarlos él. |
| R-08 | El LLM interpreta mal una instrucción de horario (fecha u horas equivocadas). | Validación determinística, resumen explícito con fecha y horas en 24 h, confirmación obligatoria y vencimiento de la solicitud. |
| R-09 | El administrador olvida un cierre "hasta nuevo aviso" y el servicio queda cerrado. | Recordatorio diario al comenzar el horario base (`admin_schedule_still_closed`) y comando `estado`. |
| R-10 | Un pedido de un día ya reportado se cancela o se libera después del envío. | Corrección automática del reporte cuando el pedido vuelve a estar resuelto (CU-17); si queda publicado sin tomar, la corrección espera y el administrador puede cancelarlo. |
| R-11 | WhatsApp no permite eliminar para todos (mensaje viejo, cambio de política, error de Evolution). | Respaldo: editar a "❌ CANCELADO" y, si tampoco se puede, responder citando; aviso al administrador si todo falla. Reacciones tardías sobre el pedido cancelado se ignoran (T19). |
| R-12 | Un motero recibe "Asignado" y enseguida "cancelado" (carrera cancelar/tomar). | Es el comportamiento correcto y ocurre en ese orden (FIFO por destinatario); el texto de cancelación incluye el detalle completo para que no haya dudas. |
| R-13 | Un local confirma tarde (fuera de horario o con el borrador vencido) y cree que el pedido salió. | Al confirmar fuera de horario se responde explícitamente que no se envió (`store_order_not_sent_closed`); al vencer se avisa (`store_draft_expired`). |
| R-14 | Una apertura extra pasada la medianoche mezcla pedidos de dos fechas calendario. | Día operativo con corte configurable; las horas después de medianoche se muestran como del día siguiente en el resumen de confirmación. |
| R-15 | Muchas correcciones si se liberan o cancelan pedidos de días viejos. | Solo se envía una corrección cuando hay cambio neto y no quedan pendientes de esa fecha; varias modificaciones se acumulan en una sola corrección. |
| R-16 | El administrador extiende un día ya reportado y espera ver los pedidos nuevos en el reporte original. | El resumen de confirmación avisa que irán en una corrección y a qué hora; la corrección lista cada pedido agregado. |
| R-17 | El secreto del webhook viaja en la ruta (`/webhooks/evolution/{secret}`) y Evolution guarda esa URL en su base y la escribe en sus logs de error al fallar una entrega; quien acceda al manager, la API o la base de Evolution puede leerlo. | Nuestros logs lo reemplazan por `***` (`RedactWebhookSecretFilter`) y en desarrollo se quitó `WEBHOOKS` del `LOG_LEVEL` de Evolution. En producción (Dokku): manager y API de Evolution solo con HTTPS, `AUTHENTICATION_API_KEY` fuerte y acceso restringido (idealmente no expuestos públicamente o detrás de autenticación/lista de IPs); el webhook usa la red interna de Dokku; base y logs de Evolution con acceso restringido; rotar `WEBHOOK_SECRET` (y reconfigurar el webhook de la instancia) si se sospecha una filtración. |
| R-18 | Envío duplicado: `send_outbound` mantiene el lock de la fila mientras llama a Evolution; si el worker se cae (o se corta la conexión con la base) después de que Evolution envió el mensaje y antes del `COMMIT`, la fila vuelve a `pending` y el barrido la reenvía. | Ventana muy corta (una llamada HTTP); el `COMMIT` va inmediatamente después del envío y no hay más trabajo en esa transacción. Un duplicado repite un aviso pero no cambia el estado de ningún pedido (los cambios de estado ya se confirmaron antes de encolar). Una `2xx` con respuesta inutilizable no se reintenta (puede haberse enviado). Si se vuelve un problema real: marcar `sending` en una transacción previa y, ante una fila `sending` vieja, consultar a Evolution por el mensaje antes de reenviar en vez de reenviar a ciegas. |

## 6. Variables de entorno

### Aplicación (`web` y `worker`)

| Variable | Ejemplo / defecto | Descripción |
|----------|-------------------|-------------|
| `APP_ENV` | `development` | `development` \| `production`. |
| `LOG_LEVEL` | `INFO` | |
| `TZ` | `America/Argentina/Buenos_Aires` | Zona para horario, cierres, fecha del reporte y horas mostradas. |
| `DATABASE_URL` | `postgresql+asyncpg://yavoy:yavoy@postgres:5432/yavoy` | Inyectada por Dokku en producción. |
| `REDIS_URL` | `redis://redis:6379/0` | Inyectada por Dokku en producción. |
| `EVOLUTION_BASE_URL` | `http://evolution:8080` | |
| `EVOLUTION_API_KEY` | — | API key global de Evolution. |
| `EVOLUTION_INSTANCE` | `yavoy` | Nombre de la instancia. |
| `WEBHOOK_SECRET` | — | Secreto en la ruta del webhook. |
| `COURIERS_GROUP_JID` | `1203630xxxxxxxx@g.us` | Grupo de moteros; cualquier otro grupo se ignora. |
| `ADMIN_PHONES` | `5491100000000` | Número(s) del administrador, separados por coma: alertas, reporte, comandos y cambios de horario. |
| `LLM_PROVIDER` | `openai` | Proveedor de LLM: `openai` (u `openai-compatible`, cualquier API compatible con Chat Completions de OpenAI y salida `json_schema`) o `fake` (solo desarrollo, rechazado en producción: extractor determinístico por etiquetas `Campo: valor`, sin red). |
| `LLM_MODEL` | — | Modelo para extracción. Obligatorio en producción (`APP_ENV=production`): sin él la configuración no valida y el deploy falla. |
| `LLM_API_KEY` | — | Obligatorio en producción, igual que `WEBHOOK_SECRET`: sin él la configuración no valida y el deploy falla. En desarrollo es opcional: el worker arranca igual, registra `LLM not configured` en `ERROR` y cada pedido recibe `store_processing_error` (y los administradores `admin_llm_failure`); o se usa `LLM_PROVIDER=fake`. Los tests nunca lo usan. |
| `LLM_BASE_URL` | vacío | Para proveedores compatibles con OpenAI o modelos propios. |
| `LLM_TIMEOUT_SECONDS` | `20` | |
| `LLM_MAX_RETRIES` | `3` | |
| `ORDER_CODE_PREFIX` | `YV-` | |
| `BUSINESS_HOURS_START` | `09:00` | Inicio del horario base (en `TZ`). |
| `BUSINESS_HOURS_END` | `23:00` | Fin del horario base (en `TZ`). Las aperturas extra se suman y los cierres se restan. El reporte se genera al cierre efectivo del día. |
| `BUSINESS_DAY_ROLLOVER_TIME` | `05:00` | Hora en que empieza un nuevo día operativo; lo anterior pertenece al día previo. |
| `SCHEDULE_EARLIEST_OPEN_TIME` | `06:00` | Apertura más temprana permitida para una apertura extra. |
| `SCHEDULE_LATEST_CLOSE_TIME` | `02:00` | Cierre más tardío permitido para una apertura extra (del día calendario siguiente). |
| `SCHEDULE_CONFIRMATION_TTL_MINUTES` | `5` | Plazo para confirmar un cambio de horario. |
| `OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES` | `30` | Mínimo entre respuestas "fuera de horario" al mismo local. |
| `DRAFT_TTL_MINUTES` | `30` | Vencimiento del borrador o de la confirmación pendiente por inactividad. |
| `DRAFT_DEBOUNCE_SECONDS` | `8` | Intervalo de agrupación de mensajes. |
| `MAX_MISSING_FIELDS_PROMPTS` | `2` | Tras cuántos pedidos de datos se agrega el ejemplo de formato. |
| `UNCLAIMED_ALERT_MINUTES` | `20` | Minutos sin toma antes de alertar al administrador (una vez). |
| `NOTIFY_LOSER_ON_RACE` | `true` | Avisar "ya lo tomó otro". |
| `NOTIFY_REACTION_ON_CANCELLED` | `true` | Avisar por privado al motero que reacciona a un pedido cancelado (una vez por pedido y motero). |
| `REPORT_PENDING_REMINDER_MINUTES` | `120` | Minutos después del cierre efectivo del día en que, si el reporte sigue retenido, se recuerda al administrador (una vez; 01:00 en un día normal). |
| `DAILY_REPORT_MAX_ATTEMPTS` | `3` | |
| `STORE_FALLBACK_NAME` | `Local` | Nombre si el local no tiene `pushName`. |
| `COURIER_FALLBACK_NAME` | `Motero` | Nombre si el motero no tiene `pushName`. |
| `SENDER_RATE_LIMIT_PER_MINUTE` | `20` | Mensajes privados por remitente y minuto antes de ignorarlos. |
| `HELP_REPLY_COOLDOWN_MINUTES` | `30` | |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | Frecuencia de las tareas programadas. |
| `INBOUND_RECOVERY_SECONDS` | `60` | Antigüedad para reencolar eventos no procesados. |
| `OUTBOUND_RECOVERY_SECONDS` | `60` | Antigüedad para reencolar mensajes salientes en `pending` (barrido de la bandeja de salida). |
| `OUTBOUND_MIN_INTERVAL_MS` | `1000` | Separación mínima entre envíos. |
| `OUTBOUND_MAX_ATTEMPTS` | `5` | |
| `DATA_RETENTION_DAYS` | `90` | Anonimización de datos. |
| `CAPTURE_INBOUND_PAYLOADS` | `0` | Solo desarrollo (en producción se rechaza): guarda cada payload crudo del webhook en `CAPTURE_DIR` para la verificación con número real. |
| `CAPTURE_DIR` | `data/captures` | Carpeta de capturas (ignorada por git). |

### Evolution API (referencia; nombres de la imagen oficial, se confirman al fijar versión)

| Variable | Ejemplo | Descripción |
|----------|---------|-------------|
| `SERVER_URL` | `http://localhost:8080` | URL pública del manager. |
| `AUTHENTICATION_API_KEY` | = `EVOLUTION_API_KEY` | |
| `DATABASE_PROVIDER` | `postgresql` | |
| `DATABASE_CONNECTION_URI` | `postgresql://yavoy:yavoy@postgres:5432/evolution` | |
| `CACHE_REDIS_ENABLED` | `true` | |
| `CACHE_REDIS_URI` | `redis://redis:6379/1` | |
| `CACHE_REDIS_PREFIX_KEY` | `evolution` | |
| `WEBHOOK_GLOBAL_ENABLED` | `false` | El webhook se configura por instancia apuntando a `web`, solo con `MESSAGES_UPSERT` y `CONNECTION_UPDATE`. |
