# 04 — Diagramas de secuencia

Participantes comunes:

- **EVO**: Evolution API (instancia conectada al número del bot).
- **API**: servicio FastAPI (`web`); solo recibe webhooks y encola.
- **Q**: cola arq sobre Redis.
- **W**: worker arq (lógica de negocio, envíos y tareas programadas).
- **AD**: administrador (número en `ADMIN_PHONES`).
- **DB**: PostgreSQL.
- **LLM**: proveedor de LLM para extracción estructurada.

## 1. Recepción, alta automática del local, validación y confirmación del pedido

Caso: un local nuevo manda el ejemplo canónico sin la línea de pago, lo completa en un segundo mensaje y confirma el resumen.

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
        W->>Q: SET lock:store:{store_id} token NX PX TTL (TTL > peor caso del LLM)
        W->>DB: transacción corta: SELECT draft FOR UPDATE, revisión = last_store_message_at, renovar draft_expires_at, COMMIT
        Note over W,LLM: sin conexión a la base durante la llamada
        W->>LLM: extract(text, current_draft) -> OrderExtraction
        LLM-->>W: pickup_address, delivery_address, product
        W->>DB: transacción corta: SELECT draft FOR UPDATE, ¿misma revisión?
        alt misma revisión (camino normal)
            W->>DB: INSERT orders ON CONFLICT DO NOTHING si no había borrador, UPDATE orders (merge, draft_expires_at, last_store_message_at), INSERT messages, COMMIT
        else cambió (solo si se perdió el lock)
            W->>W: volver a leer el borrador y reintentar (máximo 3)
        end
        W->>Q: enqueue evaluate_draft(order_id) defer DRAFT_DEBOUNCE_SECONDS
        W->>Q: release lock (solo si el token es el propio)
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
    W->>DB: UPDATE orders SET status = 'awaiting_confirmation', confirmation_requested_at WHERE status = 'draft'
    W->>DB: INSERT outbound_messages (store_order_confirm_request)
    W->>EVO: sendText al local
    EVO->>S: "Revisá tu pedido: ... ¿Confirmás el pedido? Respondé SÍ para enviarlo o corregí lo que haga falta"

    S->>EVO: "sí"
    EVO->>API: POST /webhooks/evolution/{secret}
    API->>DB: INSERT inbound_events
    API->>Q: enqueue process_inbound_event
    API-->>EVO: 200 OK
    Q->>W: process_inbound_event
    Note over W: "sí" es un comando: no se llama al LLM
    W->>DB: is_accepting_orders(now)
    alt dentro del horario efectivo
        rect rgb(235, 245, 255)
            Note over W,DB: Una transacción
            W->>DB: UPDATE orders SET status = 'published', code, confirmed_at, published_at, unclaimed_alert_at WHERE status = 'awaiting_confirmation'
            W->>DB: INSERT order_events (confirmed, published)
            W->>DB: INSERT outbound_messages (group_order_post, store_order_published)
        end
        W->>EVO: sendText al grupo de moteros
        EVO-->>W: key.id del mensaje publicado
        rect rgb(235, 245, 255)
            Note over W,DB: Una transacción (la de send_outbound)
            W->>DB: pg_advisory_xact_lock(key.id), luego SELECT orders ... FOR UPDATE
            W->>DB: UPDATE orders SET group_message_id = key.id, unclaimed_alert_at
            W->>DB: aplicar las reacciones estacionadas para key.id (orden: messageTimestamp, inbound_event_id)
        end
        W->>EVO: sendText al local
        EVO->>S: "Pedido YV-0042 enviado a los moteros"
    else fuera del horario efectivo
        W->>EVO: store_order_not_sent_closed
        EVO->>S: "No pude enviar tu pedido: hoy no tomamos pedidos por lluvia"
    end
```

Puntos clave:

- El webhook nunca llama al LLM ni a Evolution: solo persiste y encola (RNF-01).
- La extracción del LLM recibe el borrador actual como contexto, pero la combinación y la validación las hace el código.
- Nada se publica sin el "sí" del local sobre el resumen vigente; si en lugar de "sí" el local manda una corrección,
  el pedido vuelve a `draft`, se combina y se reenvía el resumen (T05).
- El intervalo de agrupación (`DRAFT_DEBOUNCE_SECONDS`) evita responder "faltan datos" a cada mensaje de una ráfaga.
- `group_message_id` se guarda cuando Evolution devuelve el `key.id`; sin ese id no se pueden asociar las
  reacciones, por eso la publicación en el grupo se envía antes que la confirmación al local.
- Una reacción que llega entre la respuesta de Evolution y el commit de ese id no se descarta: se
  estaciona (`parked_reactions`) y se aplica primero en esa misma transacción (sección 2b).

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

## 2b. Reacción antes de que se guarde `group_message_id`

Caso: Emiliano reacciona apenas ve la publicación; su evento se procesa antes de que `send_outbound`
haga commit del `key.id`. Joel reacciona después, cuando el id ya está guardado.

```mermaid
sequenceDiagram
    autonumber
    actor A as Emiliano
    actor B as Joel
    participant EVO as Evolution API
    participant WS as Worker (send_outbound)
    participant WR as Worker (reacciones)
    participant DB as PostgreSQL

    WS->>EVO: sendText al grupo (group_order_post)
    EVO-->>WS: key.id = P
    A->>EVO: reacciona 👍 a P (messageTimestamp t1)
    EVO->>WR: reacción de A a P
    rect rgb(255, 245, 235)
        Note over WR,DB: Transacción de la reacción de A
        WR->>DB: pg_advisory_xact_lock(P)
        WR->>DB: SELECT orders WHERE group_message_id = P → ninguno (aún sin commit)
        WR->>DB: INSERT parked_reactions (P, A, t1, pending)
    end
    WR->>WR: encola retry_parked_reaction diferido 1 s
    rect rgb(235, 245, 255)
        Note over WS,DB: Transacción de send_outbound
        WS->>DB: pg_advisory_xact_lock(P), SELECT orders FOR UPDATE
        WS->>DB: UPDATE orders SET group_message_id = P
        WS->>DB: aplica las estacionadas de P: UPDATE ... WHERE courier_id IS NULL → gana A
    end
    B->>EVO: reacciona 🏍️ a P (t2 > t1)
    EVO->>WR: reacción de B a P
    WR->>DB: pg_advisory_xact_lock(P), encuentra el pedido, UPDATE ... → 0 filas (T11)
    WR->>WR: retry_parked_reaction de A: ya aplicada, no hace nada
```

- El lock de asesoramiento (por transacción, sobre el id del mensaje) cierra la ventana entre "ningún
  pedido tiene este id" y "la reacción quedó estacionada": o la reacción ve el id ya confirmado, o queda
  estacionada antes de que el gancho busque las estacionadas.
- Si el id nunca aparece (la reacción era a otro mensaje), los reintentos (1 s, 2 s, 2 s) la descartan
  y lo registran en el log. Un mensaje del bot que no es publicación y ya tiene id se ignora en el acto.

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
    CR->>EVO: group_order_delete (eliminar la publicación para todos)
    EVO->>G: (la publicación de YV-0042 desaparece)
    CR->>EVO: admin_command_ok y store_order_cancelled_by_admin
```

## 5. Cancelación con carrera contra una toma

Caso: el local cancela YV-0042 en el mismo instante en que Emiliano reacciona. Se muestran los dos órdenes posibles.

```mermaid
sequenceDiagram
    autonumber
    actor S as Local
    actor C as Motero (Emiliano)
    participant EVO as Evolution API
    participant W1 as Worker (cancelación)
    participant W2 as Worker (toma)
    participant DB as PostgreSQL
    participant G as Grupo de moteros

    Note over S,G: Las llamadas al webhook y la cola se omiten por claridad
    S->>EVO: "cancelar" (citando la confirmación de YV-0042)
    C->>G: reacciona 👍 a la publicación de YV-0042
    EVO->>W1: store_cancel
    EVO->>W2: reaction_added

    alt la cancelación toma el lock primero
        W1->>DB: SELECT status FROM orders FOR UPDATE (published)
        W2->>DB: UPDATE orders SET courier_id ... WHERE status = 'published' (espera el lock)
        W1->>DB: UPDATE orders SET status = 'cancelled' WHERE status = 'published'
        W1->>DB: INSERT outbound_messages (group_order_delete, store_order_cancelled)
        W1->>DB: COMMIT
        DB-->>W2: 0 filas (status = 'cancelled')
        W2->>DB: INSERT order_events (reaction_on_cancelled)
        W2->>EVO: courier_order_no_longer_available (si NOTIFY_REACTION_ON_CANCELLED)
        EVO->>C: "El pedido YV-0042 fue cancelado"
    else la toma se confirma primero
        W2->>DB: UPDATE orders SET courier_id = Emiliano, status = 'assigned' WHERE status = 'published'
        W2->>DB: INSERT outbound_messages (courier_order_assigned, store_order_assigned, group_order_assigned)
        W2->>DB: COMMIT
        W1->>DB: SELECT status, courier_id FROM orders FOR UPDATE (assigned, Emiliano)
        W1->>DB: UPDATE orders SET status = 'cancelled', courier_id = NULL WHERE status = 'assigned'
        W1->>DB: INSERT outbound_messages (courier_order_cancelled, group_order_delete x2, store_order_cancelled)
        W1->>DB: COMMIT
        Note over EVO,C: Envíos FIFO por destinatario: primero la asignación, después la cancelación
        EVO->>C: "Te asignamos el pedido YV-0042 ..."
        EVO->>C: "Tu pedido YV-0042 ha sido cancelado: retiro, entrega, producto, abona"
    end

    W1->>EVO: DELETE /chat/deleteMessageForEveryone (publicación y respuestas del bot)
    alt eliminado
        EVO-->>W1: OK
        W1->>DB: INSERT order_events (group_post_deleted)
    else falla
        EVO-->>W1: error
        W1->>EVO: editar mensaje a "CANCELADO — Pedido YV-0042" (o responder citándolo)
        W1->>DB: INSERT order_events (group_post_delete_failed)
    end
    W1->>EVO: store_order_cancelled
    EVO->>S: "Pedido YV-0042 cancelado"
```

## 6. Reporte diario (con espera de pendientes y corrección)

```mermaid
sequenceDiagram
    autonumber
    participant CR as Worker (cron)
    participant DB as PostgreSQL
    participant X as Generador xlsx (openpyxl)
    participant EVO as Evolution API
    actor AD as Administrador

    Note over CR: Cada SCHEDULER_INTERVAL_SECONDS
    CR->>CR: due = report_due_at(D) (cierre efectivo, 23:00 o fin de la apertura extra)
    opt now >= due
        CR->>DB: INSERT daily_reports (report_date = D, status = 'waiting') ON CONFLICT DO NOTHING
        CR->>DB: SELECT daily_reports WHERE report_date = D FOR UPDATE SKIP LOCKED
        CR->>DB: SELECT pendientes FROM orders WHERE business_date = D AND status IN ('draft', 'awaiting_confirmation', 'published')
        alt hay pendientes
            CR->>DB: UPDATE daily_reports SET pending_orders_count, last_checked_at
            opt now >= due + REPORT_PENDING_REMINDER_MINUTES y sin recordatorio
                CR->>EVO: admin_report_pending_reminder (códigos)
                EVO->>AD: "El reporte del 09/10 espera: YV-0057 sin tomar"
            end
        else sin pendientes
            CR->>DB: INSERT report_revisions (revision = 1, kind = 'initial', snapshot)
            CR->>X: build_daily_report(summary_rows, order_rows)
            X-->>CR: bytes del .xlsx
            CR->>DB: UPDATE report_revisions SET file_content, status = 'generated'
            CR->>DB: INSERT outbound_messages (admin_daily_report, document, daily_report:D:v1:phone)
            CR->>EVO: POST /message/sendMedia
            EVO->>AD: documento + "Reporte del 09/10"
            CR->>DB: UPDATE daily_reports SET status = 'sent', current_revision = 1
        end
    end

    Note over CR,AD: 23:40 Joel quita su reacción a YV-0123 (pedido del 09/10)
    CR->>DB: (en la transacción de la liberación) UPDATE daily_reports SET status = 'correction_pending' WHERE status = 'sent'
    Note over CR,AD: 23:45 Emiliano toma YV-0123
    CR->>DB: SELECT daily_reports WHERE status = 'correction_pending' AND due_at <= now() FOR UPDATE SKIP LOCKED
    CR->>DB: ¿pendientes de D? No
    CR->>DB: comparar pedidos de D con snapshot de la revisión 1
    DB-->>CR: YV-0123 courier Joel pasa a Emiliano
    CR->>DB: INSERT report_revisions (revision = 2, kind = 'correction', changes, snapshot)
    CR->>X: build_daily_report(..., changes)
    X-->>CR: bytes del .xlsx (con hoja Correcciones)
    CR->>DB: INSERT outbound_messages (admin_daily_report_correction, daily_report:D:v2:phone)
    CR->>EVO: POST /message/sendMedia
    EVO->>AD: "Corrección del reporte del 09/10 (versión 2): el pedido YV-0123 pasó de Joel a Emiliano"
    CR->>DB: UPDATE daily_reports SET status = 'sent', current_revision = 2
```

## 7. Cambio de horario por el administrador (con confirmación)

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
    Note over W: SET lock:admin:{teléfono} NX PX TTL (TTL > peor caso del LLM)
    W->>DB: transacción corta: solicitud pendiente del administrador (revisión), COMMIT
    Note over W,LLM: sin conexión a la base durante la llamada
    W->>LLM: parse_schedule(text, now en TZ, día operativo, horario base) -> ScheduleOverrideIntent
    LLM-->>W: kind = closed_today, reason = "lluvia"
    rect rgb(235, 245, 255)
        Note over W,DB: Una transacción corta
        W->>DB: SELECT solicitud pendiente FOR UPDATE: ¿misma revisión? (si no, reintentar)
        W->>W: validate_schedule_intent() con los cambios activos; ends_at = fin del día operativo (05:00)
        W->>DB: UPDATE schedule_overrides SET status = 'superseded' WHERE created_by = AD AND status = 'pending_confirmation'
        W->>DB: INSERT schedule_overrides (status = 'pending_confirmation', confirmation_expires_at = now + 5 min)
        W->>DB: INSERT outbound_messages (admin_schedule_confirm)
    end
    Note over W: release lock (solo con el token propio)
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

    Note over AD,S: Las aperturas siguen el mismo flujo. "hoy hasta las 00" genera kind = open_window<br/>(23:00 a 24:00) y corre la hora del reporte del día a las 24:00<br/>(si el reporte ya se envió, lo reabre: ver sección 8)
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

## 8. Apertura extra después de enviado el reporte (corrección con pedidos agregados)

```mermaid
sequenceDiagram
    autonumber
    actor AD as Administrador
    participant W as Worker
    participant DB as PostgreSQL
    participant CR as Worker (cron)
    participant EVO as Evolution API
    actor S as Local
    actor C as Motero (Joel)

    Note over AD,C: 23:05 el reporte del 09/10 (revisión 1) ya se envió
    AD->>W: (vía Evolution) "hoy hasta la 01"
    W->>W: parse y validate_schedule_intent: open_window 23:00 a 01:00 del 09/10 (dentro de 06:00 a 02:00)
    W->>DB: INSERT schedule_overrides (open_window, pending_confirmation)
    W->>EVO: admin_schedule_confirm
    EVO->>AD: "Hoy 09/10 se toman pedidos también hasta la 01:00 (del 10/10). El reporte ya se envió: los pedidos nuevos irán en una corrección a la 01:00. ¿Lo aplico?"
    AD->>W: (vía Evolution) "sí"
    rect rgb(235, 245, 255)
        Note over W,DB: Una transacción
        W->>DB: UPDATE schedule_overrides SET status = 'active', confirmed_at
        W->>DB: SELECT daily_reports WHERE report_date = '09/10' FOR UPDATE
        W->>DB: UPDATE daily_reports SET status = 'correction_pending', due_at = 01:00, reopened_by_override_id, pending_reminder_sent_at = NULL
    end
    W->>EVO: admin_schedule_applied

    S->>W: (vía Evolution) pedido nuevo a las 23:40, confirma con "sí"
    W->>DB: orders YV-0130 (business_date = 09/10) published
    C->>W: (vía Evolution) reacción a YV-0130
    W->>DB: YV-0130 assigned a Joel

    loop cada SCHEDULER_INTERVAL_SECONDS
        CR->>DB: SELECT daily_reports WHERE status = 'correction_pending' AND due_at <= now() FOR UPDATE SKIP LOCKED
    end
    Note over CR: 01:00 vence due_at
    CR->>DB: ¿pendientes del 09/10?
    alt hay pendientes
        opt now >= due_at + REPORT_PENDING_REMINDER_MINUTES y sin recordatorio
            CR->>EVO: admin_report_pending_reminder (variante corrección)
        end
    else sin pendientes
        CR->>DB: diff_snapshots(revisión 1, actual) devuelve added YV-0130 (Joel)
        CR->>DB: INSERT report_revisions (revision = 2, kind = 'correction', changes, snapshot)
        CR->>DB: INSERT outbound_messages (admin_daily_report_correction, daily_report:09/10:v2:phone)
        CR->>EVO: POST /message/sendMedia
        EVO->>AD: "Corrección del reporte del 09/10 (versión 2): pedido agregado YV-0130 (tomado por Joel)"
        CR->>DB: UPDATE daily_reports SET status = 'sent', current_revision = 2, reopened_by_override_id = NULL
    end
```

Si a la 01:00 no hubo pedidos nuevos ni otros cambios, `diff_snapshots` devuelve una lista vacía: no se crea
revisión y el reporte vuelve a `sent` sin enviar nada (PA-01).

## 9. Envío de mensajes salientes (bandeja de salida)

Todos los envíos siguen el mismo patrón, que no se repite en los diagramas anteriores:

1. En la misma transacción que el cambio de estado se inserta en `outbound_messages` con una
   `idempotency_key` determinística (por ejemplo `order:42:claimed:courier:7`). Si ya existe, no se inserta.
2. Después del `COMMIT`, se encola `send_outbound(id)`.
3. `send_outbound` toma la fila con `FOR UPDATE SKIP LOCKED`, respeta el límite de envío
   (`OUTBOUND_MIN_INTERVAL_MS`, lock global en Redis) y el orden por destinatario (no envía una fila si hay
   otra anterior pendiente para el mismo `recipient_jid`), llama a Evolution según `message_type`
   (`text` → `sendText`, `document` → `sendMedia`, `delete` → `deleteMessageForEveryone`, `edit` → `updateMessage`)
   y guarda `wa_message_id` y `status = 'sent'`.
4. Ante un error transitorio (red o timeout, `5xx`, `408`, `429`) reintenta con backoff hasta
   `OUTBOUND_MAX_ATTEMPTS`; un error permanente (cualquier otro `4xx`) no se reintenta. En ambos casos termina
   en `status = 'failed'` y alerta al administrador (`admin_outbound_failed`, salvo que la fila fallida sea
   esa alerta). Detalle en [arquitectura](06-arquitectura.md), "Política de reintentos de la bandeja de salida".
5. Una tarea de barrido reencola filas `pending` con más de `OUTBOUND_RECOVERY_SECONDS` (por si se perdió el trabajo en Redis).
