# 07 — Catálogo de mensajes

Todos los mensajes que envía el bot. La clave (`template_key`) es la que usa el código y se guarda en
`outbound_messages.template_key`. Los textos usan formato de WhatsApp (`*negrita*`, `_cursiva_`) y
variables `{así}`. Los textos son una primera propuesta para revisar con el cliente.

Variables comunes:

| Variable | Origen |
|----------|--------|
| `{code}` | `orders.code` (`YV-0042`) |
| `{store_name}` | `stores.name` |
| `{courier_name}`, `{courier_phone}` | `couriers.name`, `couriers.phone` (formato legible) |
| `{order_details}` | Bloque de detalle del pedido (ver abajo) |
| `{missing_list}` | Lista de campos faltantes o inválidos, uno por línea |
| `{active_codes}` | Lista de códigos de pedidos activos |
| `{minutes}` | Minutos configurados |
| `{status_label}` | Estado legible: `delivered` → "entregado", `cancelled` → "cancelado", `expired` → "vencido" |

Bloque `{order_details}` (se reutiliza en varios mensajes):

```
🏪 *Retira en:* {store_name} — {pickup_address}
👤 *Cliente:* {customer_name} — {customer_phone}
📍 *Entregar en:* {delivery_address}
📦 *Pedido:* {items_description}
💵 *Cobrar:* {amount_to_collect} ({payment_method_label})
📝 *Notas:* {notes}
```

La línea de notas se omite si no hay notas. `payment_method_label`: `cash` → "efectivo", `transfer` →
"transferencia", `prepaid` → "ya pagado". En la publicación del grupo, el teléfono del cliente se
omite (solo lo recibe el motero asignado; ver PA-03).

Nombres legibles de campos para `{missing_list}`:

| Campo | Texto |
|-------|-------|
| `customer_name` | Nombre del cliente |
| `customer_phone` | Teléfono del cliente |
| `delivery_address` | Dirección de entrega (calle y altura) |
| `items_description` | Qué hay que llevar |
| `amount_to_collect` | Monto a cobrar (0 si ya está pagado) |
| `payment_method` | Medio de pago (efectivo, transferencia o ya pagado) |

Motivos de invalidez: `invalid_phone` → "el teléfono no parece válido", `invalid_amount` → "el monto no
es un número válido", `payment_mismatch` → "indicaste que está pagado pero hay un monto a cobrar".

## Mensajes al local

| Clave | Disparador | Texto |
|-------|------------|-------|
| `store_order_missing_fields` | T03: borrador evaluado con datos faltantes o inválidos | Recibí tu pedido, pero me falta:<br/>{missing_list}<br/><br/>Mandame esos datos en uno o varios mensajes. Para descartarlo, escribí *cancelar*. |
| `store_order_format_example` | T03 cuando `missing_prompts_count >= MAX_MISSING_FIELDS_PROMPTS` | Si te sirve, podés mandarlo así:<br/>_Cliente: Ana Pérez_<br/>_Tel: 11 5555 1234_<br/>_Dirección: Mitre 1200, 3° B_<br/>_Pedido: 2 pizzas muzzarella_<br/>_Cobrar: 15000 efectivo_<br/>_Notas: timbre no anda_ |
| `store_order_published` | T04: pedido publicado | ✅ Pedido *{code}* publicado. Te aviso cuando un motero lo tome.<br/><br/>{order_details}<br/><br/>Para cancelarlo respondé este mensaje con *cancelar*. |
| `store_order_assigned` | T07: un motero tomó el pedido | 🏍️ *{courier_name}* tomó tu pedido *{code}*. Teléfono: {courier_phone}. |
| `store_order_unassigned` | T10 / T17: el motero liberó el pedido o lo liberó el admin | ⚠️ {courier_name} liberó tu pedido *{code}*. Ya lo volvimos a ofrecer a los moteros. |
| `store_order_reposted` | T12: republicación por falta de toma | ⏳ Todavía nadie tomó tu pedido *{code}*. Lo volvimos a publicar. |
| `store_order_unclaimed` | T13: se agotaron las republicaciones | ⚠️ Tu pedido *{code}* sigue sin motero después de {minutes} minutos. Ya avisamos a la administración. Sigue publicado por si alguien lo toma. |
| `store_order_delivered` | T16: entregado | 📦 Tu pedido *{code}* fue entregado por {courier_name}. |
| `store_order_cancelled` | T14 / T15: cancelado | ❌ Pedido *{code}* cancelado. |
| `store_draft_cancelled` | T06: borrador descartado | Listo, descarté el pedido que estabas cargando. |
| `store_draft_expired` | T05: borrador vencido | ⌛ Descarté el pedido que estabas cargando porque pasaron {minutes} minutos sin datos nuevos. Si todavía lo necesitás, mandalo de nuevo completo. |
| `store_cancel_ambiguous` | CU-08 2a: `cancelar` sin código con varios pedidos activos | Tenés varios pedidos activos: {active_codes}. Escribí *cancelar* y el código, por ejemplo *cancelar {code}*. |
| `store_order_not_found` | CU-08 2b: código inexistente o ajeno | No encontré un pedido activo con ese código. Tus pedidos activos: {active_codes}. |
| `store_cancel_not_allowed` | CU-08 3a: pedido entregado o ya cancelado | El pedido *{code}* ya está {status_label} y no se puede cancelar. |
| `store_help` | CU-01 5b: mensaje sin datos de pedido (con límite de frecuencia) | Hola 👋 Para pedir un envío mandame: nombre y teléfono del cliente, dirección de entrega, qué hay que llevar, monto a cobrar y medio de pago. Para cancelar: *cancelar* y el código del pedido. |
| `store_unsupported_media` | CU-01: audio, imagen sin texto, sticker | Por ahora solo puedo leer mensajes de texto. ¿Me lo mandás escrito? |
| `store_processing_error` | CU-01 5a: falla persistente del LLM | Tuve un problema para leer tu mensaje. Probá mandarlo de nuevo en unos minutos. |

## Mensajes al grupo de moteros

| Clave | Disparador | Texto |
|-------|------------|-------|
| `group_order_post` | T04: publicación inicial | 🆕 *Pedido {code}*<br/><br/>{order_details_public}<br/><br/>👉 *Reaccioná a este mensaje para tomarlo.* |
| `group_order_repost` | T12: republicación | 🔁 *Sigue sin motero: {code}*<br/><br/>{order_details_public}<br/><br/>👉 Reaccioná a este mensaje para tomarlo. |
| `group_order_assigned` | T07 (citando la publicación) | ✅ *{code}* asignado a *{courier_name}*. |
| `group_order_unassigned` | T10 / T17 (citando la publicación) | 🔓 *{code}* vuelve a estar disponible. Reaccioná de nuevo para tomarlo (si ya habías reaccionado, cambiá el emoji). |
| `group_order_cancelled` | T14 / T15 (citando la publicación) | ❌ *{code}* fue cancelado por el local. No hace falta retirarlo. |
| `group_order_delivered` | T16, si `ANNOUNCE_DELIVERED_IN_GROUP` | 📦 *{code}* entregado por {courier_name}. |

`{order_details_public}` es `{order_details}` sin el teléfono del cliente.

## Mensajes al motero (privado)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `courier_order_assigned` | T07: ganó la toma | ✅ *Te asignamos el pedido {code}*<br/><br/>{order_details}<br/><br/>Cuando lo entregues respondé este mensaje con *entregado*. Si no podés hacerlo, quitá tu reacción en el grupo. |
| `courier_order_already_taken` | T08, si `NOTIFY_LOSER_ON_RACE` (una vez por motero y pedido) | El pedido *{code}* ya lo tomó otro motero. ¡Gracias igual! |
| `courier_order_released` | T10 / T17: liberó el pedido | Liberaste el pedido *{code}*. Ya no está a tu nombre. |
| `courier_order_cancelled` | T15: el local canceló un pedido asignado | ❌ El local canceló el pedido *{code}*. Si ya lo retiraste, coordiná la devolución con {store_name}. |
| `courier_order_delivered_ack` | T16 | 👍 Registré el pedido *{code}* como entregado. |
| `courier_delivered_ambiguous` | CU-09 2a | Tenés varios pedidos asignados: {active_codes}. Escribí *entregado* y el código, por ejemplo *entregado {code}*. |
| `courier_order_not_found` | CU-09 2b | No encontré un pedido asignado a vos con ese código. Tus pedidos asignados: {active_codes}. |
| `courier_delivery_reminder` | T18 | ⏰ ¿Pudiste entregar el pedido *{code}*? Si ya lo entregaste respondé *entregado {code}*. |
| `courier_help` | CU-09: otro texto de un motero (con límite de frecuencia) | Para tomar pedidos reaccioná a la publicación en el grupo. Para cerrar uno escribime *entregado* y el código. |

## Mensajes a remitentes no registrados

| Clave | Disparador | Texto |
|-------|------------|-------|
| `unknown_sender` | CU-10 (máximo uno cada `UNKNOWN_SENDER_REPLY_COOLDOWN_HOURS`) | Hola 👋 Este número recibe pedidos solo de locales adheridos. Si querés sumarte, comunicate con la administración. |

## Alertas al administrador (privado a `ADMIN_PHONES`)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_order_unclaimed` | T13 | ⚠️ El pedido *{code}* de {store_name} sigue sin motero tras {reposts} republicaciones. |
| `admin_delivery_overdue` | T18 | ⏰ El pedido *{code}* está asignado a {courier_name} hace más de {minutes} minutos y no se marcó como entregado. |
| `admin_unknown_sender` | CU-10 (con límite de frecuencia por número) | ℹ️ Escribió un número no registrado: {phone}. Mensaje: "{preview}". |
| `admin_unknown_reactor` | CU-03 3a/3b: reacción de alguien no identificado como motero | ℹ️ Alguien no registrado como motero ({participant}) reaccionó al pedido *{code}*. Se ignoró. |
| `admin_outbound_failed` | Envío con `status = 'failed'` | ❗ No se pudo enviar el mensaje `{template_key}` a {recipient} tras {attempts} intentos. |
| `admin_whatsapp_disconnected` | Evento `connection.update` con estado cerrado | ❗ El número del bot se desconectó de WhatsApp. Hay que volver a vincularlo desde el manager. |

## Reglas de envío

- Los mensajes al grupo derivados de una publicación (`group_order_assigned`, `group_order_unassigned`,
  `group_order_cancelled`) se envían **citando** la última publicación del pedido, para que el contexto
  quede claro en el hilo del grupo.
- `store_order_published` y `courier_order_assigned` guardan su `wa_message_id`, para que el local
  (*cancelar*) y el motero (*entregado*) puedan responder citándolos.
- Nunca se envía el mismo `template_key` dos veces para el mismo evento: lo impide `idempotency_key`.
- Comandos reconocidos (sin distinguir mayúsculas ni tildes): local → `cancelar [código]`, `ayuda`;
  motero → `entregado [código]`, `ayuda`.
