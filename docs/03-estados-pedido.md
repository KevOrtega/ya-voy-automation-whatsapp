# 03 — Máquina de estados del pedido

Casi toda la lógica del sistema es esta máquina de estados. Cada transición se implementa como un
`UPDATE` condicional sobre `orders` (la condición `WHERE` es la guarda) dentro de una transacción
que además inserta en `order_events` y encola los mensajes en `outbound_messages`. Si el `UPDATE`
no devuelve filas, la transición no ocurrió y no se envía nada.

## Estados

| Estado | Descripción | ¿Terminal? |
|--------|-------------|------------|
| `draft` | Borrador en construcción; faltan datos o se está esperando el intervalo de agrupación. | No |
| `published` | Completo y publicado en el grupo (una sola publicación); esperando que un motero lo tome. | No |
| `assigned` | Tomado por un motero (`courier_id` no nulo). | No |
| `delivered` | Marcado como entregado por el motero o cerrado por el administrador. | Sí |
| `cancelled` | Cancelado por el local o el administrador. | Sí |
| `expired` | Borrador vencido por inactividad del local. | Sí |

"Sin tomar" no es un estado: es un pedido `published` con `unclaimed_alert_sent_at` no nulo, que
indica que ya se alertó al administrador (RN-18). Sigue pudiendo tomarse igual que cualquier pedido
publicado.

Invariante: `courier_id IS NOT NULL` si y solo si `status IN ('assigned', 'delivered')`
(se implementa como `CHECK`, ver [modelo de datos](05-modelo-datos.md)).

## Diagrama

```mermaid
stateDiagram-v2
    direction LR
    [*] --> draft: store_message_received [within business hours]

    draft --> draft: store_message_received / merge, extend TTL
    draft --> published: draft_evaluated [all required fields present]
    draft --> expired: draft_ttl_elapsed
    draft --> cancelled: store_cancel

    published --> assigned: reaction_added [courier_id IS NULL]
    published --> published: unclaimed_alert_due / alert admin once
    published --> cancelled: store_cancel / admin_cancel

    assigned --> published: reaction_removed [remover = assigned courier]
    assigned --> published: admin_release
    assigned --> assigned: delivery_reminder_due / remind once
    assigned --> delivered: courier_delivered [sender = assigned courier]
    assigned --> delivered: admin_close
    assigned --> cancelled: store_cancel / admin_cancel

    delivered --> [*]
    cancelled --> [*]
    expired --> [*]
```

Eventos que **no** producen transición (se registran como ignorados, sin mensajes salvo los indicados):

- `store_message_received` fuera de horario → `store_out_of_hours` (con límite de frecuencia), no se crea ni modifica el borrador.
- `reaction_added` de otro motero cuando el pedido está `assigned` → `courier_order_already_taken` (una vez por motero y pedido).
- `reaction_added` del mismo motero asignado (cambio de emoji) → nada.
- `reaction_removed` de alguien que no es el asignado, o con el pedido en un estado distinto de `assigned` → nada.
- Cualquier reacción sobre un pedido `delivered` o `cancelled` → nada.
- `courier_delivered` de un motero que no es el asignado → `courier_order_not_found`.

## Tabla de transiciones

| # | Desde | Evento | Guarda (condición) | Hacia | Acciones | Mensajes enviados |
|---|-------|--------|--------------------|-------|----------|-------------------|
| T01 | — | `store_message_received` | Dentro de horario; no es comando; el local no tiene borrador abierto | `draft` | Alta automática del local si no existe; crear pedido; extraer con LLM; `draft_expires_at = now() + DRAFT_TTL`; programar evaluación | — |
| T02 | `draft` | `store_message_received` | Dentro de horario; no es comando | `draft` | Extraer, combinar campos, renovar `draft_expires_at`, reprogramar evaluación | — |
| T03 | `draft` | `draft_evaluated` | Falta algún campo obligatorio | `draft` | `missing_prompts_count += 1` | `store_order_missing_fields` (+ `store_order_format_example` si corresponde) |
| T04 | `draft` | `draft_evaluated` | `pickup_address`, `delivery_address`, `product` y `payment_method` presentes | `published` | Asignar `code`, `published_at`, `unclaimed_alert_at = now() + UNCLAIMED_ALERT`; publicar y guardar `group_message_id` | `group_order_post`, `store_order_published` |
| T05 | `draft` | `draft_ttl_elapsed` | `draft_expires_at < now()` | `expired` | — | `store_draft_expired` |
| T06 | `draft` | `store_cancel` | Pedido del local | `cancelled` | `cancelled_at`, `cancelled_by = 'store'` | `store_draft_cancelled` |
| T07 | `published` | `reaction_added` | Motero no bloqueado y `courier_id IS NULL` | `assigned` | Alta automática del motero si no existe; `courier_id`, `last_courier_id`, `assigned_at` | `courier_order_assigned`, `store_order_assigned`, `group_order_assigned` |
| T08 | `assigned` | `reaction_added` | Motero distinto del asignado | (sin cambio) | Registrar `claim_rejected` | `courier_order_already_taken` si `NOTIFY_LOSER_ON_RACE` (una vez por motero y pedido) |
| T09 | `assigned` | `reaction_added` | Mismo motero asignado (cambio de emoji) | (sin cambio) | — | — |
| T10 | `assigned` | `reaction_removed` | `courier_id = remitente` | `published` | `courier_id = NULL`, `assigned_at = NULL`, reiniciar `unclaimed_alert_at`, `unclaimed_alert_sent_at = NULL` | `store_order_unassigned`, `group_order_unassigned`, `courier_order_released` |
| T11 | `assigned` | `reaction_removed` | `courier_id <> remitente` | (sin cambio) | Registrar como ignorado | — |
| T12 | `published` | `unclaimed_alert_due` | `unclaimed_alert_at < now()` y `unclaimed_alert_sent_at IS NULL` | `published` | `unclaimed_alert_sent_at = now()` | `admin_order_unclaimed` |
| T13 | `published` | `store_cancel` / `admin_cancel` | Pedido del local (o admin) | `cancelled` | `cancelled_at`, `cancelled_by` | `store_order_cancelled` o `store_order_cancelled_by_admin`, `group_order_cancelled` (+ `admin_command_ok`) |
| T14 | `assigned` | `store_cancel` / `admin_cancel` | Pedido del local (o admin) | `cancelled` | `cancelled_at`, `cancelled_by`, `courier_id = NULL` (queda en `last_courier_id`) | Ídem T13 + `courier_order_cancelled` |
| T15 | `assigned` | `courier_delivered` | `courier_id = remitente` | `delivered` | `delivered_at`, `delivered_by = 'courier'` | `courier_order_delivered_ack`, `store_order_delivered`, `group_order_delivered` (opcional) |
| T16 | `assigned` | `admin_release` | — | `published` | Igual que T10 | Igual que T10 + `admin_command_ok` |
| T17 | `assigned` | `admin_close` | — | `delivered` | `delivered_at`, `delivered_by = 'admin'` | `store_order_delivered`, `courier_order_closed_by_admin`, `admin_command_ok` |
| T18 | `assigned` | `delivery_reminder_due` | `assigned_at < now() - DELIVERY_REMINDER` y `delivery_reminder_sent_at IS NULL` | `assigned` | `delivery_reminder_sent_at = now()` | `courier_delivery_reminder`, `admin_delivery_overdue` |

## Implementación de referencia de las guardas

```sql
-- T07: claim (also covers T08/T09 when no row is returned)
UPDATE orders
   SET courier_id = :courier_id, last_courier_id = :courier_id,
       status = 'assigned', assigned_at = now(), updated_at = now()
 WHERE id = :order_id
   AND courier_id IS NULL
   AND status = 'published'
RETURNING id;

-- T10: release by reaction removal
UPDATE orders
   SET courier_id = NULL, status = 'published', assigned_at = NULL,
       unclaimed_alert_at = now() + make_interval(mins => :unclaimed_alert_minutes),
       unclaimed_alert_sent_at = NULL, updated_at = now()
 WHERE id = :order_id
   AND courier_id = :courier_id
   AND status = 'assigned'
RETURNING id;

-- T12: unclaimed alert (one per publication period)
UPDATE orders
   SET unclaimed_alert_sent_at = now(), updated_at = now()
 WHERE id = :order_id
   AND status = 'published'
   AND unclaimed_alert_sent_at IS NULL
   AND unclaimed_alert_at < now()
RETURNING id;

-- T15: delivered
UPDATE orders
   SET status = 'delivered', delivered_at = now(), delivered_by = 'courier', updated_at = now()
 WHERE id = :order_id
   AND courier_id = :courier_id
   AND status = 'assigned'
RETURNING id;
```

Cuando el `UPDATE` de T07 no devuelve filas, se lee el pedido para decidir entre T08 (otro motero),
T09 (mismo motero) o "pedido no disponible" (terminal), solo para elegir qué mensaje enviar; nunca
para decidir la asignación.
