# 06 — Arquitectura

## 1. Contexto (C4 nivel 1)

```mermaid
flowchart TB
    store(["👤 Local<br/>Envía pedidos por WhatsApp"])
    courier(["👤 Motero<br/>Toma pedidos reaccionando en el grupo"])
    admin(["👤 Administrador<br/>Recibe alertas y el reporte diario,<br/>interviene pedidos con comandos"])

    yavoy["<b>Ya Voy</b><br/>Sistema de despacho de pedidos por WhatsApp"]

    wa[("WhatsApp<br/>(red de Meta)")]
    llm["Proveedor de LLM<br/>Extracción estructurada"]

    store -- "mensajes privados" --> wa
    courier -- "reacciones en el grupo,<br/>comandos por privado" --> wa
    admin -- "comandos por privado" --> wa
    wa <-- "eventos y envíos<br/>(protocolo de WhatsApp Web)" --> yavoy
    yavoy -- "HTTPS: texto a OrderExtraction" --> llm
    wa -- "alertas y reporte .xlsx" --> admin
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
    worker -- "REST: sendText, sendMedia" --> evo
    worker -- "HTTPS" --> llm
    evo -- "SQL" --> pg
    evo -- "caché" --> redis
```

### Responsabilidades

| Contenedor | Tecnología | Responsabilidad | No hace |
|------------|-----------|-----------------|---------|
| `web` | FastAPI + Uvicorn, SQLAlchemy 2 (async) + asyncpg | Validar el secreto del webhook, descartar lo obviamente irrelevante (`fromMe`, otros grupos, estados), insertar en `inbound_events`, encolar, responder rápido. `/health`. | Llamar al LLM o a Evolution. No expone API de administración. |
| `worker` | arq, openpyxl | `process_inbound_event`, `evaluate_draft`, `send_outbound`; tareas cron: vencimiento de borradores, alerta de pedidos sin tomar, recordatorio de entrega, reporte diario, retención y barridos de recuperación. | Exponer HTTP. |
| `postgres` | PostgreSQL 16 | Fuente de verdad. Dos bases: `yavoy` (aplicación) y `evolution` (interna de Evolution). | — |
| `redis` | Redis 7 | Cola de arq, lock por local, lock del límite de envío, contadores de límite por remitente, caché de Evolution (con prefijo propio). | Guardar estado de negocio. |
| `evolution` | Evolution API v2 | Mantener la sesión de WhatsApp (QR), entregar eventos por webhook, enviar textos y documentos. | Lógica de negocio. |

### Tareas programadas (arq)

Todas corren en el `worker` como una única tarea cron cada `SCHEDULER_INTERVAL_SECONDS` (o una por
tarea con el mismo intervalo). Son idempotentes y "recuperables": comparan contra la hora actual en
lugar de depender de ejecutarse en un minuto exacto, así que si el worker estuvo caído hacen lo pendiente al volver.

| Tarea | Condición | Efecto |
|-------|-----------|--------|
| `expire_drafts` | `draft` con `draft_expires_at < now()` | T05 |
| `alert_unclaimed_orders` | `published`, sin alerta, `unclaimed_alert_at < now()` | T12 |
| `remind_pending_deliveries` | `assigned` hace más de `DELIVERY_REMINDER_MINUTES`, sin recordatorio | T18 |
| `send_daily_report` | Hora local ≥ `DAILY_REPORT_TIME` y no hay `daily_reports` enviado para hoy | CU-14 |
| `recover_inbound_events` | `inbound_events` en `received` con más de `INBOUND_RECOVERY_SECONDS` | Reencolar |
| `recover_outbound_messages` | `outbound_messages` en `pending` antiguos | Reencolar |
| `apply_data_retention` | Una vez por día | Anonimizar lo más viejo que `DATA_RETENTION_DAYS` |

### Componentes internos (paquete `ya_voy`, propuesta)

```
ya_voy/
  api/            # FastAPI app, routers: webhooks, health
  worker/         # arq WorkerSettings, job functions, scheduled tasks
  domain/         # order state machine, validation, command parsing, business hours (pure code, no I/O)
  identity/       # sender classification, store/courier auto-registration, lid/phone resolution and merge
  extraction/     # LLM client interface + OrderExtraction schema + prompt + payment normalization
  whatsapp/       # Evolution API client, webhook payload parsing (jid/lid helpers)
  messaging/      # templates catalog, outbox enqueue/render
  reports/        # daily report queries and xlsx builder (openpyxl)
  db/             # SQLAlchemy models, repositories, Alembic migrations
  config.py       # pydantic-settings
```

- **`domain`** no depende de FastAPI, arq ni Evolution: recibe eventos normalizados
  (`StoreTextReceived`, `ReactionAdded`, `ReactionRemoved`, `CourierCommandReceived`,
  `AdminCommandReceived`, ...) y decide transiciones. Es lo que más se testea.
- **`identity`** clasifica al remitente (admin, motero, local) y hace el alta automática. Concentra todo
  lo que depende de cómo WhatsApp expone `@lid` y números, que es lo más cambiante.
- **`extraction`** define `OrderExtractor.extract(text, current) -> OrderExtraction`. La implementación
  concreta usa un cliente compatible con varios proveedores (por ejemplo `pydantic-ai`, `instructor` o
  `litellm`; se elige al implementar). Proveedor y modelo por configuración (RNF-13).
- **`whatsapp`** encapsula el formato de Evolution: normaliza números, JID y `@lid` (incluidos los campos
  alternativos), detecta reacciones (`message.reactionMessage`, con `text` vacío = remoción) y mensajes
  citados (`contextInfo.stanzaId`).
- **`reports`** arma las filas con SQL y genera el `.xlsx` en memoria con openpyxl (encabezados en negrita,
  columnas de fecha con formato `dd/mm/yyyy hh:mm`, filtros automáticos).

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
| D-08 | Administración solo por comandos de WhatsApp | El administrador ya vive en WhatsApp; evita construir y asegurar un panel en v1. |
| D-09 | Tareas programadas por comparación con la hora actual y registros de idempotencia | Funcionan aunque el worker se reinicie; el reporte no se duplica ni se pierde. |

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
- Para probar horario y reporte se puede fijar la hora con una variable de desarrollo (`FAKE_NOW`) o con `BUSINESS_HOURS_*` y `DAILY_REPORT_TIME` cercanos a la hora actual.
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
- **Zona horaria:** `TZ` se configura en la app (`dokku config:set yavoy TZ=America/Argentina/Buenos_Aires`); la aplicación igual calcula horario y jornada con `zoneinfo` a partir de `TZ`, sin depender del reloj del VPS.
- **Escalado:** un solo proceso `worker` alcanza al inicio; las garantías de corrección están en Postgres, así que se puede escalar sin cambios.
- **Backups:** `dokku postgres:backup-schedule` para ambas bases. La carpeta de instancias de Evolution contiene la sesión de WhatsApp: perderla obliga a escanear el QR de nuevo.

## 5. Riesgos

| # | Riesgo | Mitigación |
|---|--------|------------|
| R-01 | Bloqueo del número (Baileys no es oficial). | Número dedicado y "calentado", límite de envío, sin difusión masiva, sin iniciar conversaciones con desconocidos. |
| R-02 | Sin registro previo, cualquiera que conozca el número puede cargar pedidos que llegan al grupo. | Límite por remitente, bloqueo manual (`is_blocked`), nombre del local visible en el grupo y en el reporte, alerta al admin ante abuso. |
| R-03 | Moteros conocidos solo por `@lid`: el reporte no tiene su teléfono y el envío de privados a un `@lid` depende de la versión de Baileys/Evolution. | Guardar el número cuando el evento lo trae (`participantAlt`, `senderPn`, etc.); verificar con eventos reales el envío a `@lid`; si un privado falla, `admin_outbound_failed` y el grupo igual muestra "Asignado a". |
| R-04 | La misma persona vista una vez por `@lid` y otra por número genera dos moteros. | Fusión automática cuando un evento trae ambos identificadores (ver [modelo de datos](05-modelo-datos.md)). |
| R-05 | Un motero que nunca reaccionó escribe `entregado` por privado y se lo trata como local. | No tiene pedidos asignados, así que no hay efecto dañino; el mensaje se registra. Se revisa con PA-02. |
| R-06 | El administrador escribe desde un chat que WhatsApp expone solo como `@lid` y no se lo reconoce como admin. | Comparar también contra el número alternativo del evento; verificar con su número real al armar el entorno; agregar `ADMIN_JIDS` si hiciera falta. |
| R-07 | Pedidos publicados cerca del cierre quedan sin tomar después de las 23:00. | La alerta al administrador funciona a cualquier hora (RF-08). |

## 6. Variables de entorno

### Aplicación (`web` y `worker`)

| Variable | Ejemplo / defecto | Descripción |
|----------|-------------------|-------------|
| `APP_ENV` | `development` | `development` \| `production`. |
| `LOG_LEVEL` | `INFO` | |
| `TZ` | `America/Argentina/Buenos_Aires` | Zona para horario de operación, jornada del reporte y horas mostradas. |
| `DATABASE_URL` | `postgresql+asyncpg://yavoy:yavoy@postgres:5432/yavoy` | Inyectada por Dokku en producción. |
| `REDIS_URL` | `redis://redis:6379/0` | Inyectada por Dokku en producción. |
| `EVOLUTION_BASE_URL` | `http://evolution:8080` | |
| `EVOLUTION_API_KEY` | — | API key global de Evolution. |
| `EVOLUTION_INSTANCE` | `yavoy` | Nombre de la instancia. |
| `WEBHOOK_SECRET` | — | Secreto en la ruta del webhook. |
| `COURIERS_GROUP_JID` | `1203630xxxxxxxx@g.us` | Grupo de moteros; cualquier otro grupo se ignora. |
| `ADMIN_PHONES` | `5491100000000` | Número(s) del administrador, separados por coma: alertas, reporte y comandos. |
| `LLM_PROVIDER` | `openai` | Proveedor de LLM. |
| `LLM_MODEL` | — | Modelo para extracción. |
| `LLM_API_KEY` | — | |
| `LLM_BASE_URL` | vacío | Para proveedores compatibles con OpenAI o modelos propios. |
| `LLM_TIMEOUT_SECONDS` | `20` | |
| `LLM_MAX_RETRIES` | `3` | |
| `ORDER_CODE_PREFIX` | `YV-` | |
| `BUSINESS_HOURS_START` | `09:00` | Inicio del horario de operación (en `TZ`). |
| `BUSINESS_HOURS_END` | `23:00` | Fin del horario de operación (en `TZ`). |
| `OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES` | `30` | Mínimo entre respuestas "fuera de horario" al mismo local. |
| `DRAFT_TTL_MINUTES` | `30` | Vencimiento del borrador por inactividad. |
| `DRAFT_DEBOUNCE_SECONDS` | `8` | Intervalo de agrupación de mensajes. |
| `MAX_MISSING_FIELDS_PROMPTS` | `2` | Tras cuántos pedidos de datos se agrega el ejemplo de formato. |
| `UNCLAIMED_ALERT_MINUTES` | `20` | Minutos sin toma antes de alertar al administrador (una vez). |
| `NOTIFY_LOSER_ON_RACE` | `true` | Avisar "ya lo tomó otro". |
| `ANNOUNCE_DELIVERED_IN_GROUP` | `false` | Anunciar entregas en el grupo. |
| `DELIVERY_REMINDER_MINUTES` | `90` | Recordatorio al motero y alerta al admin. |
| `DAILY_REPORT_TIME` | `23:00` | Hora del reporte diario y fin de la jornada (en `TZ`). |
| `DAILY_REPORT_MAX_ATTEMPTS` | `3` | |
| `STORE_FALLBACK_NAME` | `Local` | Nombre si el local no tiene `pushName`. |
| `COURIER_FALLBACK_NAME` | `Motero` | Nombre si el motero no tiene `pushName`. |
| `SENDER_RATE_LIMIT_PER_MINUTE` | `20` | Mensajes privados por remitente y minuto antes de ignorarlos. |
| `HELP_REPLY_COOLDOWN_MINUTES` | `30` | |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | Frecuencia de las tareas programadas. |
| `INBOUND_RECOVERY_SECONDS` | `60` | Antigüedad para reencolar eventos no procesados. |
| `OUTBOUND_MIN_INTERVAL_MS` | `1000` | Separación mínima entre envíos. |
| `OUTBOUND_MAX_ATTEMPTS` | `5` | |
| `DATA_RETENTION_DAYS` | `90` | Anonimización de datos. |

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
