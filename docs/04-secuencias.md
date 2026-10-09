# 04 — Diagramas de secuencia

Participantes comunes:

- **EVO**: Evolution API (instancia conectada al número del bot).
- **API**: servicio FastAPI (`web`); solo recibe webhooks y encola.
- **Q**: cola arq sobre Redis.
- **W**: worker arq (lógica de negocio, envíos y tareas programadas).
- **AD**: administrador (número en `ADMIN_PHONES`).
- **DB**: PostgreSQL.
- **LLM**: proveedor de LLM para extracción estructurada.

## 1. Recepción, alta automática del local y validación del pedido

Caso: un local nuevo manda el ejemplo canónico sin la línea de pago y lo completa en un segundo mensaje.

```mermaid
sequenceDiagram
    autonumber
    actor S as Local
    participant EVO as Evolution API
    participant API as FastAPI (web)
    participant Q as Redis / arq
    participant W as Worker
    participant DB as PostgreSQL
    participant LLM as LLM

    S->>EVO: "Dirección de retiro: casa nogal almendros / Dirección de entrega: el ceibo 2871 / Producto/: almacen"
    EVO->>API: POST /webhooks/evolution/{secret} (messages.upsert)
    API->>DB: INSERT inbound_events ON CONFLICT (wa_message_id) DO NOTHING
    alt evento nuevo
        DB-->>API: id
        API->>Q: enqueue process_inbound_event(id)
    else duplicado
        DB-->>API: (sin filas)
    end
    API-->>EVO: 200 OK

    Q->>W: process_inbound_event(id)
    Note over W: Filtrado: no es fromMe, es chat privado,<br/>no está bloqueado, dentro del límite de frecuencia
    W->>DB: ¿remitente en ADMIN_PHONES o en couriers?
    DB-->>W: no
    W->>DB: INSERT stores (wa_jid, phone, display_name = pushName) ON CONFLICT DO UPDATE
    W->>DB: is_accepting_orders(now): horario base en TZ menos schedule_overrides activos
    alt fuera del horario efectivo
        W->>DB: INSERT messages, INSERT outbound_messages (store_out_of_hours o store_closed_*) si no hubo uno reciente
        W->>EVO: sendText al local
    else dentro del horario efectivo
        W->>Q: acquire lock store:{store_id}
        W->>DB: SELECT draft FOR UPDATE, INSERT orders (status = 'draft') si no existe
        W->>DB: INSERT messages (direction = 'in')
        W->>LLM: extract(text, current_draft) -> OrderExtraction
        LLM-->>W: pickup_address, delivery_address, product
        W->>DB: UPDATE orders SET merged fields, draft_expires_at, last_store_message_at
        W->>Q: enqueue evaluate_draft(order_id) defer DRAFT_DEBOUNCE_SECONDS
        W->>Q: release lock
    end

    Q->>W: evaluate_draft(order_id)
    W->>DB: SELECT order FOR UPDATE
    Note over W: Si llegó otro mensaje después de programar<br/>(last_store_message_at más nuevo), termina sin hacer nada
    W->>W: validate_required_fields() falta payment_method
    W->>DB: INSERT outbound_messages (store_order_missing_fields)
    W->>EVO: POST /message/sendText (al local)
    EVO->>S: "Me falta: medio de pago"

    S->>EVO: "transferencia"
    EVO->>API: POST /webhooks/evolution/{secret}
    API->>DB: INSERT inbound_events
    API->>Q: enqueue process_inbound_event
    API-->>EVO: 200 OK
    Q->>W: process_inbound_event
    W->>LLM: extract(text, current_draft)
    LLM-->>W: payment_method = transfer, payment_method_raw = "transferencia"
    W->>DB: UPDATE orders (merge)
    W->>Q: enqueue evaluate_draft defer

    Q->>W: evaluate_draft(order_id)
    W->>W: validate_required_fields() OK
    rect rgb(235, 245, 255)
        Note over W,DB: Una transacción
        W->>DB: UPDATE orders SET status = 'published', code, published_at, unclaimed_alert_at WHERE status = 'draft'
        W->>DB: INSERT order_events (published)
        W->>DB: INSERT outbound_messages (group_order_post, store_order_published)
    end
    W->>EVO: sendText al grupo de moteros
    EVO-->>W: key.id del mensaje publicado
    W->>DB: UPDATE orders SET group_message_id = key.id
    W->>EVO: sendText al local
    EVO->>S: "Pedido YV-0042 publicado"
```

Puntos clave:

- El webhook nunca llama al LLM ni a Evolution: solo persiste y encola (RNF-01).
- La extracción del LLM recibe el borrador actual como contexto, pero la combinación y la validación las hace el código.
- El intervalo de agrupación (`DRAFT_DEBOUNCE_SECONDS`) evita responder "faltan datos" a cada mensaje de una ráfaga.
- `group_message_id` se guarda cuando Evolution devuelve el `key.id`; sin ese id no se pueden asociar las
  reacciones, por eso la publicación en el grupo se envía antes que la confirmación al local.

## 2. Toma con carrera entre dos moteros y alta automática

```mermaid
sequenceDiagram
    autonumber
    actor A as Motero A (nuevo, solo lid)
    actor B as Motero B (conocido)
    participant G as Grupo de moteros
    participant EVO as Evolution API
    participant API as FastAPI (web)
    participant W1 as Worker (job 1)
    participant W2 as Worker (job 2)
    participant DB as PostgreSQL
    actor S as Local

    A->>G: reacciona 👍 a "Pedido YV-0042"
    B->>G: reacciona 🏍️ a "Pedido YV-0042"
    G->>EVO: dos eventos de reacción
    par webhook A
        EVO->>API: messages.upsert (reactionMessage, participant = A@lid, pushName = Emiliano)
        API->>DB: INSERT inbound_events
        API-->>EVO: 200
        API->>W1: enqueue
    and webhook B
        EVO->>API: messages.upsert (reactionMessage, participant = B@s.whatsapp.net)
        API->>DB: INSERT inbound_events
        API-->>EVO: 200
        API->>W2: enqueue
    end

    W1->>DB: SELECT orders WHERE group_message_id = reactionMessage.key.id
    W2->>DB: SELECT orders WHERE group_message_id = reactionMessage.key.id
    W1->>DB: upsert couriers (lid = A@lid, phone si viene participantAlt, display_name = Emiliano)
    W2->>DB: SELECT couriers WHERE phone_jid = B, actualiza display_name si cambió
    par carrera
        W1->>DB: UPDATE orders SET courier_id = A ... WHERE courier_id IS NULL RETURNING id
    and
        W2->>DB: UPDATE orders SET courier_id = B ... WHERE courier_id IS NULL RETURNING id
    end
    Note over DB: Postgres bloquea la fila: el segundo UPDATE espera,<br/>reevalúa courier_id IS NULL y no afecta filas
    DB-->>W1: 1 fila (ganó A)
    DB-->>W2: 0 filas (perdió B)

    W1->>DB: INSERT order_events (claimed) + outbound_messages
    W1->>EVO: privado a A: courier_order_assigned (detalle, sin teléfonos)
    EVO->>A: "Te asignamos el pedido YV-0042 ..."
    W1->>EVO: al local: store_order_assigned
    EVO->>S: "Tu motero para este pedido será Emiliano"
    W1->>EVO: al grupo, citando la publicación: group_order_assigned
    EVO->>G: "YV-0042 asignado a Emiliano"

    W2->>DB: INSERT order_events (claim_rejected)
    W2->>EVO: privado a B: courier_order_already_taken
    EVO->>B: "El pedido YV-0042 ya lo tomó otro motero"
```

## 3. Liberación, quitar reacción de un no asignado y cambio de emoji

```mermaid
sequenceDiagram
    autonumber
    actor A as Motero A (asignado)
    actor B as Motero B
    participant EVO as Evolution API
    participant W as Worker
    participant DB as PostgreSQL
    participant G as Grupo de moteros
    actor S as Local

    Note over A,S: Las llamadas al webhook y la cola se omiten por claridad

    B->>EVO: quita su reacción (reactionMessage.text vacío)
    EVO->>W: reaction_removed (B)
    W->>DB: UPDATE orders ... WHERE courier_id = B AND status = 'assigned' RETURNING id
    DB-->>W: 0 filas
    Note over W: Sin efecto. inbound_events.status = 'ignored'

    A->>EVO: cambia 👍 por 🔥
    EVO->>W: reaction_added (A, 🔥)
    W->>DB: UPDATE orders ... WHERE courier_id IS NULL RETURNING id
    DB-->>W: 0 filas
    W->>DB: SELECT courier_id FROM orders
    Note over W: courier_id = A: es cambio de emoji, sin efecto

    A->>EVO: quita su reacción
    EVO->>W: reaction_removed (A)
    W->>DB: UPDATE orders SET courier_id = NULL, status = 'published', reinicia unclaimed_alert_at WHERE courier_id = A AND status = 'assigned'
    DB-->>W: 1 fila
    W->>DB: INSERT order_events (released_by_courier) + outbound_messages
    W->>EVO: courier_order_released, store_order_unassigned, group_order_unassigned
    EVO->>A: "Liberaste el pedido YV-0042"
    EVO->>S: "Emiliano liberó tu pedido YV-0042, lo volvimos a ofrecer"
    EVO->>G: "YV-0042 vuelve a estar disponible. Reaccioná para tomarlo"
```

## 4. Alerta por pedido sin tomar

```mermaid
sequenceDiagram
    autonumber
    participant CR as Worker (cron)
    participant DB as PostgreSQL
    participant EVO as Evolution API
    actor AD as Administrador
    participant G as Grupo de moteros

    loop cada SCHEDULER_INTERVAL_SECONDS
        CR->>DB: SELECT orders WHERE status = 'published' AND unclaimed_alert_sent_at IS NULL AND unclaimed_alert_at < now() FOR UPDATE SKIP LOCKED
        opt hay pedidos vencidos
            CR->>DB: UPDATE orders SET unclaimed_alert_sent_at = now(), INSERT order_events (unclaimed_alert_sent)
            CR->>EVO: admin_order_unclaimed
            EVO->>AD: "YV-0042 lleva 20 minutos sin motero"
        end
    end

    Note over AD,G: El pedido sigue publicado. El administrador decide:<br/>esperar, avisar a los moteros o cancelar
    AD->>EVO: "cancelar YV-0042" (por privado al bot)
    EVO->>CR: (vía webhook y cola) admin_cancel
    CR->>DB: UPDATE orders SET status = 'cancelled', cancelled_by = 'admin'
    CR->>EVO: store_order_cancelled_by_admin, group_order_cancelled, admin_command_ok
```

## 5. Reporte diario (con espera de pedidos pendientes)

```mermaid
sequenceDiagram
    autonumber
    participant CR as Worker (cron)
    participant DB as PostgreSQL
    participant X as Generador xlsx (openpyxl)
    participant EVO as Evolution API
    actor AD as Administrador

    Note over CR: Desde DAILY_REPORT_TIME (23:00) de la fecha D,<br/>cada SCHEDULER_INTERVAL_SECONDS
    CR->>DB: INSERT daily_reports (report_date = D, status = 'waiting') ON CONFLICT DO NOTHING
    CR->>DB: SELECT daily_reports WHERE report_date = D AND status = 'waiting' FOR UPDATE SKIP LOCKED
    CR->>DB: SELECT count, codes FROM orders WHERE fecha local de created_at = D AND status IN ('draft', 'published')

    alt hay pedidos pendientes
        CR->>DB: UPDATE daily_reports SET pending_orders_count, last_checked_at
        opt ya pasó REPORT_PENDING_REMINDER_TIME (01:00 de D+1) y pending_reminder_sent_at IS NULL
            CR->>DB: UPDATE daily_reports SET pending_reminder_sent_at = now()
            CR->>EVO: admin_report_pending_reminder (códigos pendientes)
            EVO->>AD: "El reporte del 09/10 espera: YV-0057, YV-0058 sin tomar"
        end
        Note over CR,AD: El administrador toma o cancela los pendientes.<br/>La próxima verificación vuelve a evaluar
    else sin pendientes
        CR->>DB: UPDATE daily_reports SET status = 'generating'
        CR->>DB: Resumen: order_events de los pedidos de D agrupados por courier_id
        CR->>DB: Pedidos: orders de D + stores + couriers
        CR->>X: build_daily_report(summary_rows, order_rows)
        X-->>CR: bytes del .xlsx
        CR->>DB: UPDATE daily_reports SET file_content, status = 'generated'
        CR->>DB: INSERT outbound_messages (admin_daily_report, document) por cada ADMIN_PHONES, idempotency_key daily_report:D:phone
        CR->>EVO: POST /message/sendMedia (document, reporte-ya-voy-AAAA-MM-DD.xlsx)
        EVO->>AD: documento + "Reporte del 09/10"
        EVO-->>CR: key.id
        CR->>DB: UPDATE outbound_messages SET status = 'sent', UPDATE daily_reports SET status = 'sent', sent_at
    end
```

## 6. Cambio de horario por el administrador (con confirmación)

```mermaid
sequenceDiagram
    autonumber
    actor AD as Administrador
    participant EVO as Evolution API
    participant W as Worker
    participant LLM as LLM
    participant DB as PostgreSQL
    actor S as Local

    Note over AD,S: Las llamadas al webhook y la cola se omiten por claridad

    AD->>EVO: "hoy no tomamos pedidos por lluvia"
    EVO->>W: mensaje privado de un número de ADMIN_PHONES
    W->>W: ¿es comando (cancelar, liberar, estado, horario, ayuda) o respuesta sí/no? No
    W->>LLM: parse_schedule(text, now en TZ, horario base) -> ScheduleOverrideIntent
    LLM-->>W: kind = closed_today, reason = "lluvia"
    W->>W: validate_schedule_intent() y calcular starts_at = now, ends_at = 24:00 local
    rect rgb(235, 245, 255)
        Note over W,DB: Una transacción
        W->>DB: UPDATE schedule_overrides SET status = 'superseded' WHERE created_by = AD AND status = 'pending_confirmation'
        W->>DB: INSERT schedule_overrides (status = 'pending_confirmation', confirmation_expires_at = now + 5 min)
        W->>DB: INSERT outbound_messages (admin_schedule_confirm)
    end
    W->>EVO: sendText al administrador
    EVO->>AD: "Entendí esto: hoy 09/10 no se toman pedidos (motivo: lluvia). ¿Lo aplico? Respondé sí o no"

    alt responde "sí" a tiempo
        AD->>EVO: "sí"
        EVO->>W: mensaje privado
        W->>DB: UPDATE schedule_overrides SET status = 'active', confirmed_at WHERE status = 'pending_confirmation' AND confirmation_expires_at > now()
        DB-->>W: 1 fila
        W->>EVO: admin_schedule_applied
        EVO->>AD: "Listo. Hoy no se toman más pedidos (motivo: lluvia)"
    else responde "no"
        AD->>EVO: "no"
        W->>DB: UPDATE status = 'rejected'
        W->>EVO: admin_schedule_rejected
    else no responde
        W->>DB: (cron) UPDATE status = 'expired' WHERE confirmation_expires_at < now()
    end

    S->>EVO: "Dirección de retiro: ..."
    EVO->>W: mensaje de local
    W->>DB: is_accepting_orders(now) devuelve cierre closed_today, reason lluvia
    W->>EVO: store_closed_today (si no se le respondió hace poco)
    EVO->>S: "Hoy no tomamos pedidos por lluvia."

    AD->>EVO: "ya tomamos pedidos de nuevo"
    W->>LLM: parse_schedule(...)
    LLM-->>W: kind = reopen
    W->>DB: INSERT schedule_overrides (kind = reopen, pending_confirmation)
    W->>EVO: admin_schedule_confirm ("Vuelvo a tomar pedidos ahora. Se levanta: cierre de hoy por lluvia")
    AD->>EVO: "sí"
    rect rgb(235, 245, 255)
        W->>DB: UPDATE reopen SET status = 'applied', confirmed_at
        W->>DB: UPDATE schedule_overrides SET status = 'cancelled', cancelled_at, cancelled_by_override_id WHERE status = 'active' AND en vigor ahora
    end
    W->>EVO: admin_schedule_applied
```

## 7. Envío de mensajes salientes (bandeja de salida)

Todos los envíos siguen el mismo patrón, que no se repite en los diagramas anteriores:

1. En la misma transacción que el cambio de estado se inserta en `outbound_messages` con una
   `idempotency_key` determinística (por ejemplo `order:42:claimed:courier:7`). Si ya existe, no se inserta.
2. Después del `COMMIT`, se encola `send_outbound(id)`.
3. `send_outbound` toma la fila con `FOR UPDATE SKIP LOCKED`, respeta el límite de envío
   (`OUTBOUND_MIN_INTERVAL_MS`, lock global en Redis), llama a Evolution (`sendText` o `sendMedia` según
   `message_type`) y guarda `wa_message_id` y `status = 'sent'`.
4. Ante error, reintenta con backoff hasta `OUTBOUND_MAX_ATTEMPTS`; luego `status = 'failed'` y alerta al administrador.
5. Una tarea de barrido reencola filas `pending` antiguas (por si se perdió el trabajo en Redis).
