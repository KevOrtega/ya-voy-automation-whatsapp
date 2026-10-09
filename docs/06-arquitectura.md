# 06 — Arquitectura

## 1. Contexto (C4 nivel 1)

```mermaid
flowchart TB
    store(["👤 Local<br/>Envía pedidos por WhatsApp"])
    courier(["👤 Motero<br/>Toma pedidos reaccionando en el grupo"])
    admin(["👤 Administrador<br/>Da de alta locales y moteros, recibe alertas"])

    yavoy["<b>Ya Voy</b><br/>Sistema de despacho de pedidos por WhatsApp"]

    wa[("WhatsApp<br/>(red de Meta)")]
    llm["Proveedor de LLM<br/>Extracción estructurada"]

    store -- "mensajes privados" --> wa
    courier -- "reacciones en el grupo,<br/>comandos por privado" --> wa
    wa <-- "eventos y envíos<br/>(protocolo de WhatsApp Web)" --> yavoy
    yavoy -- "HTTPS: texto → OrderExtraction" --> llm
    admin -- "HTTPS: API de administración" --> yavoy
    wa -- "alertas" --> admin
```

## 2. Contenedores (C4 nivel 2)

```mermaid
flowchart TB
    wa[("WhatsApp")]
    llm["Proveedor de LLM"]
    admin(["👤 Administrador"])

    subgraph sys["Ya Voy"]
        evo["<b>evolution</b><br/>Evolution API v2 (Node, Baileys)<br/>Sesión de WhatsApp del número del bot"]
        web["<b>web</b><br/>FastAPI<br/>Webhook, /health, /admin"]
        worker["<b>worker</b><br/>arq<br/>Lógica de negocio, envíos, tareas programadas"]
        pg[("<b>postgres</b><br/>PostgreSQL 16<br/>DB yavoy + DB evolution")]
        redis[("<b>redis</b><br/>Redis 7<br/>Cola arq, locks, caché de Evolution")]
    end

    wa <--> evo
    evo -- "POST /webhooks/evolution/{secret}" --> web
    web -- "INSERT inbound_events" --> pg
    web -- "enqueue" --> redis
    redis -- "jobs" --> worker
    worker -- "SQL" --> pg
    worker -- "REST: sendText, findGroupInfos" --> evo
    worker -- "HTTPS" --> llm
    evo -- "SQL" --> pg
    evo -- "caché" --> redis
    admin -- "HTTPS + ADMIN_API_KEY" --> web
```

### Responsabilidades

| Contenedor | Tecnología | Responsabilidad | No hace |
|------------|-----------|-----------------|---------|
| `web` | FastAPI + Uvicorn, SQLAlchemy 2 (async) + asyncpg | Validar el secreto del webhook, filtrar eventos irrelevantes, insertar en `inbound_events`, encolar, responder rápido. API `/admin/*`. `/health`. | Llamar al LLM o a Evolution. |
| `worker` | arq | `process_inbound_event`, `evaluate_draft`, `send_outbound`; tareas cron: republicación, vencimiento de borradores, recordatorios, barridos de recuperación. | Exponer HTTP. |
| `postgres` | PostgreSQL 16 | Fuente de verdad. Dos bases: `yavoy` (aplicación) y `evolution` (interna de Evolution). | — |
| `redis` | Redis 7 | Cola de arq, locks por local y de límite de envío, caché de Evolution (con prefijo propio). | Guardar estado de negocio. |
| `evolution` | Evolution API v2 | Mantener la sesión de WhatsApp (QR), entregar eventos por webhook, enviar mensajes. | Lógica de negocio. |

### Componentes internos (paquete `ya_voy`, propuesta)

```
ya_voy/
  api/            # FastAPI app, routers: webhooks, admin, health
  worker/         # arq WorkerSettings, job functions, cron jobs
  domain/         # order state machine, validation, commands parsing (pure code, no I/O)
  extraction/     # LLM client interface + OrderExtraction schema + prompt
  whatsapp/       # Evolution API client, webhook payload parsing (jid/lid helpers)
  messaging/      # templates catalog, outbox enqueue/render
  db/             # SQLAlchemy models, repositories, Alembic migrations
  config.py       # pydantic-settings
```

- **`domain`** no depende de FastAPI, arq ni Evolution: recibe eventos normalizados
  (`StoreTextReceived`, `ReactionAdded`, `ReactionRemoved`, `CourierCommandReceived`, ...) y decide transiciones.
  Es lo que más se testea.
- **`extraction`** define una interfaz `OrderExtractor.extract(text, current) -> OrderExtraction`.
  La implementación concreta usa un cliente compatible con varios proveedores (por ejemplo `pydantic-ai`,
  `instructor` o `litellm`; se elige al implementar). Proveedor y modelo por configuración (RNF-11).
- **`whatsapp`** encapsula el formato de Evolution: normaliza números, JID y `@lid`, detecta reacciones
  (`message.reactionMessage`, con `text` vacío = remoción) y mensajes citados (`contextInfo.stanzaId`).

### Decisiones de diseño

| # | Decisión | Motivo |
|---|----------|--------|
| D-01 | Webhook que solo persiste y encola; todo el procesamiento en el worker | Responder rápido a Evolution, tolerar picos y reintentar sin perder eventos. |
| D-02 | Asignación por `UPDATE` condicional en Postgres | Única garantía real contra la doble reacción; no depende de Redis ni del orden de llegada. |
| D-03 | Bandeja de salida (`outbound_messages`) con clave de idempotencia | Reintentos sin mensajes duplicados; registro de lo enviado; límite de envío centralizado. |
| D-04 | Lock por local en Redis + `SELECT ... FOR UPDATE` del borrador | Serializar ráfagas de mensajes del mismo local sin bloquear a los demás. |
| D-05 | LLM solo para extracción; validación y comandos en código | Respuestas predecibles, testeables y sin costo de LLM para comandos. |
| D-06 | Postgres compartido (dos bases) en desarrollo; servicios separados en producción | Simplicidad local; aislamiento en producción. |

## 3. Entorno de desarrollo (Docker Compose)

Servicios previstos en `docker-compose.yml` (se crean al armar el entorno, no en esta etapa):

| Servicio | Imagen / build | Puertos (host) | Volúmenes | Depende de |
|----------|----------------|----------------|-----------|------------|
| `postgres` | `postgres:16-alpine` | `5432` | `pg_data` | — |
| `redis` | `redis:7-alpine` | `6379` | `redis_data` | — |
| `evolution` | `evoapicloud/evolution-api:<versión fija>` | `8080` (manager y QR) | `evolution_instances` | `postgres`, `redis` |
| `web` | `build: .` — `uvicorn ya_voy.api.main:app --reload` | `8000` | código montado | `postgres`, `redis` |
| `worker` | `build: .` — `arq ya_voy.worker.WorkerSettings --watch ya_voy` | — | código montado | `postgres`, `redis` |
| `migrate` | `build: .` — `alembic upgrade head` (one-shot) | — | — | `postgres` |

Notas:

- Un script de inicialización de Postgres crea las bases `yavoy` y `evolution`.
- Evolution llama al webhook por la red interna de Compose: `http://web:8000/webhooks/evolution/<secret>`.
- La instancia de WhatsApp se crea y vincula escaneando el QR desde el manager de Evolution (`http://localhost:8080/manager`) con un número de pruebas, **nunca** el número de producción del cliente.
- Para probar sin teléfono se prevén fixtures con payloads reales de Evolution (texto, reacción, remoción de reacción, cita, `@lid`) y un comando para reenviarlos al webhook.
- Se usará el mismo `Dockerfile` para `web`, `worker` y `migrate`, que es el que Dokku construirá en producción.

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

- **Red interna:** Evolution y `yavoy` se conectan a una red de Dokku compartida (`dokku network:create yavoy-net` y `network:set ... attach-post-create`), así el webhook va a `http://yavoy.web:5000/...` y el worker llama a `http://evolution.web:<puerto>` sin salir a internet. El manager de Evolution se expone con dominio propio, HTTPS (`dokku-letsencrypt`) y `AUTHENTICATION_API_KEY`.
- **`DATABASE_URL`:** Dokku la inyecta como `postgres://...`; la configuración la convierte a `postgresql+asyncpg://...`.
- **Escalado:** un solo proceso `worker` es suficiente al inicio; la corrección no depende de eso (las garantías están en Postgres), así que se puede escalar sin cambios.
- **Backups:** `dokku postgres:backup-schedule` para ambas bases. La carpeta de instancias de Evolution contiene la sesión de WhatsApp: perderla obliga a escanear el QR de nuevo.
- **Riesgo de bloqueo:** número dedicado, calentado previamente (uso normal unos días antes), límite de envío, sin difusión masiva.

## 5. Variables de entorno

### Aplicación (`web` y `worker`)

| Variable | Ejemplo / defecto | Descripción |
|----------|-------------------|-------------|
| `APP_ENV` | `development` | `development` \| `production`. |
| `LOG_LEVEL` | `INFO` | |
| `TIMEZONE` | `America/Argentina/Buenos_Aires` | Zona para mostrar horas en mensajes. |
| `DATABASE_URL` | `postgresql+asyncpg://yavoy:yavoy@postgres:5432/yavoy` | Inyectada por Dokku en producción. |
| `REDIS_URL` | `redis://redis:6379/0` | Inyectada por Dokku en producción. |
| `EVOLUTION_BASE_URL` | `http://evolution:8080` | |
| `EVOLUTION_API_KEY` | — | API key global de Evolution. |
| `EVOLUTION_INSTANCE` | `yavoy` | Nombre de la instancia. |
| `WEBHOOK_SECRET` | — | Secreto en la ruta del webhook. |
| `COURIERS_GROUP_JID` | `1203630xxxxxxxx@g.us` | Grupo de moteros. |
| `ADMIN_API_KEY` | — | Para `/admin/*`. |
| `ADMIN_PHONES` | `5491100000000,5491100000001` | Destinatarios de alertas. |
| `LLM_PROVIDER` | `openai` | Proveedor de LLM. |
| `LLM_MODEL` | — | Modelo para extracción. |
| `LLM_API_KEY` | — | |
| `LLM_BASE_URL` | vacío | Para proveedores compatibles con OpenAI o modelos propios. |
| `LLM_TIMEOUT_SECONDS` | `20` | |
| `LLM_MAX_RETRIES` | `3` | |
| `ORDER_CODE_PREFIX` | `YV-` | |
| `DRAFT_TTL_MINUTES` | `30` | Vencimiento del borrador por inactividad. |
| `DRAFT_DEBOUNCE_SECONDS` | `8` | Intervalo de agrupación de mensajes. |
| `MAX_MISSING_FIELDS_PROMPTS` | `2` | Tras cuántos pedidos de datos se agrega un ejemplo de formato. |
| `CLAIM_TIMEOUT_MINUTES` | `10` | Tiempo sin toma antes de republicar. |
| `MAX_REPOSTS` | `2` | Republicaciones antes de pasar a `unclaimed`. |
| `NOTIFY_LOSER_ON_RACE` | `true` | Avisar "ya lo tomó otro". |
| `ANNOUNCE_DELIVERED_IN_GROUP` | `false` | Anunciar entregas en el grupo. |
| `MAX_ACTIVE_ORDERS_PER_COURIER` | `0` | `0` = sin límite. |
| `DELIVERY_REMINDER_MINUTES` | `90` | Recordatorio de entrega pendiente. |
| `UNKNOWN_SENDER_REPLY_COOLDOWN_HOURS` | `24` | |
| `HELP_REPLY_COOLDOWN_MINUTES` | `30` | |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | Frecuencia de las tareas programadas. |
| `INBOUND_RECOVERY_SECONDS` | `60` | Antigüedad para reencolar eventos no procesados. |
| `OUTBOUND_MIN_INTERVAL_MS` | `1000` | Separación mínima entre envíos. |
| `OUTBOUND_MAX_ATTEMPTS` | `5` | |
| `DATA_RETENTION_DAYS` | `90` | Anonimización de datos de clientes finales. |

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
