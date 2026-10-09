# 04 — Diagramas de secuencia

Participantes comunes:

- **WA**: WhatsApp (los teléfonos del local, los moteros y el grupo).
- **EVO**: Evolution API (instancia conectada al número del bot).
- **API**: servicio FastAPI (`web`), solo recibe webhooks y encola.
- **Q**: cola arq sobre Redis.
- **W**: worker arq (lógica de negocio y envíos).
- **DB**: PostgreSQL.
- **LLM**: proveedor de LLM para extracción estructurada.

## 1. Recepción, deduplicación y validación de un pedido

Caso: el local manda el pedido en dos mensajes; el primero queda incompleto.

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

    S->>EVO: "Envío para Ana, Mitre 1200, 2 pizzas"
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
    W->>Q: acquire lock store:{store_id}
    W->>DB: SELECT store BY phone_jid (activo)
    W->>DB: SELECT draft FOR UPDATE (store_id, status = 'draft')
    W->>DB: INSERT orders (status = 'draft') si no existe
    W->>DB: INSERT messages (direction = 'in')
    W->>LLM: extract(text, current_draft) -> OrderExtraction
    LLM-->>W: {customer_name, delivery_address, items_description}
    W->>DB: UPDATE orders SET merged fields, draft_expires_at, last_store_message_at
    W->>Q: enqueue evaluate_draft(order_id) defer DRAFT_DEBOUNCE_SECONDS
    W->>Q: release lock

    Q->>W: evaluate_draft(order_id)
    W->>DB: SELECT order FOR UPDATE
    Note over W: Si llegó otro mensaje después de programar<br/>(last_store_message_at más nuevo), termina sin hacer nada
    W->>W: validate_required_fields() (código determinístico)
    W->>DB: INSERT outbound_messages (store_order_missing_fields)
    W->>EVO: POST /message/sendText (al local)
    EVO->>S: "Me faltan: teléfono del cliente, monto a cobrar y medio de pago"

    S->>EVO: "11 5555 1234, cobrar 15000 efectivo"
    EVO->>API: POST /webhooks/evolution/{secret}
    API->>DB: INSERT inbound_events
    API->>Q: enqueue process_inbound_event
    API-->>EVO: 200 OK
    Q->>W: process_inbound_event
    W->>LLM: extract(text, current_draft)
    LLM-->>W: {customer_phone, amount_to_collect, payment_method}
    W->>DB: UPDATE orders (merge)
    W->>Q: enqueue evaluate_draft defer

    Q->>W: evaluate_draft(order_id)
    W->>W: validate_required_fields() OK
    rect rgb(235, 245, 255)
        Note over W,DB: Una transacción
        W->>DB: UPDATE orders SET status = 'published', code, published_at, claim_deadline_at WHERE status = 'draft'
        W->>DB: INSERT order_events (published)
        W->>DB: INSERT outbound_messages (group_order_post, store_order_published)
    end
    W->>EVO: sendText al grupo de moteros
    EVO-->>W: key.id del mensaje publicado
    W->>DB: INSERT group_posts (order_id, wa_message_id)
    W->>EVO: sendText al local
    EVO->>S: "Pedido YV-0042 publicado ✅"
```

Puntos clave:

- El webhook nunca llama al LLM ni a Evolution: solo persiste y encola (RNF-01).
- La extracción del LLM recibe el borrador actual como contexto, pero la combinación y la validación las hace el código.
- El intervalo de agrupación (`DRAFT_DEBOUNCE_SECONDS`) evita responder "faltan datos" a cada mensaje de una ráfaga.
- `group_posts` se inserta cuando Evolution devuelve el `key.id`; sin ese id no se pueden asociar las reacciones, por eso la publicación en el grupo se envía antes que la confirmación al local.

## 2. Toma con carrera entre dos moteros

```mermaid
sequenceDiagram
    autonumber
    actor A as Motero A
    actor B as Motero B
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
        EVO->>API: messages.upsert (reactionMessage, participant = A@lid)
        API->>DB: INSERT inbound_events
        API-->>EVO: 200
        API->>W1: enqueue
    and webhook B
        EVO->>API: messages.upsert (reactionMessage, participant = B@s.whatsapp.net)
        API->>DB: INSERT inbound_events
        API-->>EVO: 200
        API->>W2: enqueue
    end

    W1->>DB: SELECT group_posts WHERE wa_message_id = key.id
    W2->>DB: SELECT group_posts WHERE wa_message_id = key.id
    W1->>DB: SELECT couriers WHERE lid = A@lid OR phone_jid = ...
    W2->>DB: SELECT couriers WHERE phone_jid = B
    par carrera
        W1->>DB: UPDATE orders SET courier_id = A ... WHERE courier_id IS NULL RETURNING id
    and
        W2->>DB: UPDATE orders SET courier_id = B ... WHERE courier_id IS NULL RETURNING id
    end
    Note over DB: Postgres bloquea la fila: el segundo UPDATE espera,<br/>reevalúa courier_id IS NULL y no afecta filas
    DB-->>W1: 1 fila (ganó A)
    DB-->>W2: 0 filas (perdió B)

    W1->>DB: INSERT order_events (claimed) + outbound_messages
    W1->>EVO: privado a A: courier_order_assigned (detalle completo)
    EVO->>A: "Te asignamos el pedido YV-0042 ..."
    W1->>EVO: al local: store_order_assigned
    EVO->>S: "Tu pedido YV-0042 lo tomó Joel"
    W1->>EVO: al grupo, citando la publicación: group_order_assigned
    EVO->>G: "YV-0042 asignado a Joel"

    W2->>DB: INSERT order_events (claim_rejected)
    opt NOTIFY_LOSER_ON_RACE
        W2->>EVO: privado a B: courier_order_already_taken
        EVO->>B: "El pedido YV-0042 ya lo tomó otro motero"
    end
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

    B->>EVO: quita su reacción (reactionMessage.text = "")
    EVO->>W: reaction_removed (B)
    W->>DB: UPDATE orders ... WHERE courier_id = B AND status = 'assigned' RETURNING id
    DB-->>W: 0 filas
    Note over W: Sin efecto. inbound_events.status = 'ignored'

    A->>EVO: cambia 👍 por 🔥
    EVO->>W: reaction_added (A, "🔥")
    W->>DB: UPDATE orders ... WHERE courier_id IS NULL RETURNING id
    DB-->>W: 0 filas
    W->>DB: SELECT courier_id FROM orders
    Note over W: courier_id = A: es cambio de emoji, sin efecto

    A->>EVO: quita su reacción
    EVO->>W: reaction_removed (A)
    W->>DB: UPDATE orders SET courier_id = NULL, status = 'published' WHERE courier_id = A AND status = 'assigned' RETURNING id
    DB-->>W: 1 fila
    W->>DB: INSERT order_events (unclaimed_by_courier) + outbound_messages
    W->>EVO: courier_order_released (a A), store_order_unassigned (al local), group_order_unassigned (al grupo)
    EVO->>A: "Liberaste el pedido YV-0042"
    EVO->>S: "Joel liberó tu pedido YV-0042, lo volvimos a ofrecer"
    EVO->>G: "YV-0042 vuelve a estar disponible. Reaccioná para tomarlo"
```

## 4. Republicación por falta de toma

```mermaid
sequenceDiagram
    autonumber
    participant CR as Worker (cron)
    participant DB as PostgreSQL
    participant EVO as Evolution API
    participant G as Grupo de moteros
    actor S as Local
    actor AD as Administrador

    loop cada SCHEDULER_INTERVAL_SECONDS
        CR->>DB: SELECT orders WHERE status = 'published' AND claim_deadline_at < now() FOR UPDATE SKIP LOCKED
        alt repost_count < MAX_REPOSTS
            CR->>DB: UPDATE repost_count + 1, claim_deadline_at renovado
            CR->>EVO: group_order_repost
            EVO->>G: "Sigue sin motero: YV-0042 ..."
            EVO-->>CR: key.id
            CR->>DB: INSERT group_posts
            CR->>EVO: store_order_reposted
            EVO->>S: "Todavía nadie tomó YV-0042, lo volvimos a publicar"
        else repost_count >= MAX_REPOSTS
            CR->>DB: UPDATE status = 'unclaimed'
            CR->>EVO: store_order_unclaimed y admin_order_unclaimed
            EVO->>S: aviso al local
            EVO->>AD: alerta al administrador
        end
    end
```

## 5. Envío de mensajes salientes (bandeja de salida)

Todos los envíos siguen el mismo patrón, que no se repite en los diagramas anteriores:

1. En la misma transacción que el cambio de estado se inserta en `outbound_messages` con una
   `idempotency_key` determinística (por ejemplo `order:42:claimed:courier:7`). Si ya existe, no se inserta.
2. Después del `COMMIT`, se encola `send_outbound(id)`.
3. `send_outbound` toma la fila con `FOR UPDATE SKIP LOCKED`, respeta el límite de envío
   (`OUTBOUND_MIN_INTERVAL_MS`, lock global en Redis), llama a Evolution y guarda `wa_message_id` y `status = 'sent'`.
4. Ante error, reintenta con backoff hasta `OUTBOUND_MAX_ATTEMPTS`; luego `status = 'failed'` y alerta al administrador.
5. Una tarea de barrido reencola filas `pending` antiguas (por si se perdió el trabajo en Redis).
