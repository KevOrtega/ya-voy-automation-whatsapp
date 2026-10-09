# 07 — Catálogo de mensajes

Todos los mensajes que envía el bot. La clave (`template_key`) es la que usa el código y se guarda en
`outbound_messages.template_key`. Los textos usan formato de WhatsApp (`*negrita*`, `_cursiva_`) y
variables `{así}`. Son una primera propuesta para revisar con el cliente.

**Privacidad (RN-16):** ningún mensaje incluye teléfonos. Al local solo se le muestra el nombre visible
del motero; al motero, solo el detalle del pedido y el nombre del local.

Variables comunes:

| Variable | Origen |
|----------|--------|
| `{code}` | `orders.code` (`YV-0042`) |
| `{store_name}` | `stores.display_name` |
| `{courier_name}` | `couriers.display_name` |
| `{order_details}` | Bloque de detalle del pedido (ver abajo) |
| `{missing_list}` | Lista de campos faltantes, uno por línea |
| `{active_codes}` | Lista de códigos de pedidos activos |
| `{minutes}` | Minutos configurados o transcurridos |
| `{status_label}` | Estado legible: `delivered` → "entregado", `cancelled` → "cancelado", `expired` → "vencido" |
| `{hours_start}`, `{hours_end}` | `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END` |
| `{report_date}` | Fecha del reporte (`dd/mm/aaaa`) |

Bloque `{order_details}` (el mismo en el grupo, al motero y al local):

```
🏪 *Local:* {store_name}
📍 *Retiro:* {pickup_address}
📍 *Entrega:* {delivery_address}
🥪 *Producto:* {product}
💳 *Abona:* {payment_method_label}
📝 *Notas:* {notes}
```

La línea de notas se omite si no hay notas. `{payment_method_label}`: `cash` → "efectivo",
`transfer` → "transferencia", `prepaid` → "ya pagado", `other` → el texto original (`payment_method_raw`).

Con el ejemplo canónico, el bloque queda:

```
🏪 *Local:* Casa Nogal
📍 *Retiro:* casa nogal almendros
📍 *Entrega:* el ceibo 2871
🥪 *Producto:* almacen
💳 *Abona:* transferencia
```

Nombres legibles de campos para `{missing_list}`:

| Campo | Texto |
|-------|-------|
| `pickup_address` | Dirección de retiro |
| `delivery_address` | Dirección de entrega |
| `product` | Producto (qué hay que llevar) |
| `payment_method` | Cómo abona (efectivo, transferencia, ya pagado u otro) |

## Mensajes al local

| Clave | Disparador | Texto |
|-------|------------|-------|
| `store_order_missing_fields` | T03: borrador evaluado con datos faltantes | Recibí tu pedido, pero me falta:<br/>{missing_list}<br/><br/>Mandame esos datos en uno o varios mensajes. Para descartarlo, escribí *cancelar*. |
| `store_order_format_example` | T03 cuando `missing_prompts_count >= MAX_MISSING_FIELDS_PROMPTS` | Si te sirve, mandalo así:<br/>📍 Dirección de retiro: casa nogal almendros<br/>📍 Dirección de entrega: el ceibo 2871<br/>🥪 Producto: almacen<br/>💳 Abona: transferencia |
| `store_order_published` | T04: pedido publicado | ✅ Pedido *{code}* publicado. Te aviso cuando un motero lo tome.<br/><br/>{order_details}<br/><br/>Para cancelarlo, respondé este mensaje con *cancelar*. |
| `store_order_assigned` | T07: un motero tomó el pedido | 🏍️ Tu motero para el pedido *{code}* será *{courier_name}*. |
| `store_order_unassigned` | T10 / T16: el motero liberó el pedido o lo liberó el administrador | ⚠️ {courier_name} liberó tu pedido *{code}*. Ya está disponible de nuevo para los moteros. |
| `store_order_delivered` | T15 / T17: entregado o cerrado por el admin | 📦 Tu pedido *{code}* fue entregado. |
| `store_order_cancelled` | T13 / T14 por el local | ❌ Pedido *{code}* cancelado. |
| `store_order_cancelled_by_admin` | T13 / T14 por el administrador | ❌ La administración canceló tu pedido *{code}*. |
| `store_draft_cancelled` | T06: borrador descartado | Listo, descarté el pedido que estabas cargando. |
| `store_draft_expired` | T05: borrador vencido | ⌛ Descarté el pedido que estabas cargando porque pasaron {minutes} minutos sin datos nuevos. Si todavía lo necesitás, mandalo de nuevo completo. |
| `store_out_of_hours` | CU-10: mensaje fuera de horario (con límite de frecuencia) | 🌙 Estamos fuera de horario. Tomamos pedidos de {hours_start} a {hours_end}. Mandalo de nuevo cuando abramos. |
| `store_cancel_ambiguous` | CU-08 2a: `cancelar` sin código con varios pedidos activos | Tenés varios pedidos activos: {active_codes}. Escribí *cancelar* y el código, por ejemplo *cancelar {code}*. |
| `store_order_not_found` | CU-08 2b: código inexistente o ajeno | No encontré un pedido activo con ese código. Tus pedidos activos: {active_codes}. |
| `store_cancel_not_allowed` | CU-08 3a: pedido entregado o ya cancelado | El pedido *{code}* ya está {status_label} y no se puede cancelar. |
| `store_help` | CU-01 6b: mensaje sin datos de pedido (con límite de frecuencia) | Hola 👋 Para pedir un envío mandame:<br/>📍 Dirección de retiro<br/>📍 Dirección de entrega<br/>🥪 Producto<br/>💳 Cómo abona<br/><br/>Para cancelar: *cancelar* y el código del pedido. |
| `store_unsupported_media` | CU-01: audio, imagen sin texto, sticker | Por ahora solo puedo leer mensajes de texto. ¿Me lo mandás escrito? |
| `store_processing_error` | CU-01 6a: falla persistente del LLM | Tuve un problema para leer tu mensaje. Probá mandarlo de nuevo en unos minutos. |

## Mensajes al grupo de moteros

| Clave | Disparador | Texto |
|-------|------------|-------|
| `group_order_post` | T04: publicación (única) | 🆕 *Pedido {code}*<br/><br/>{order_details}<br/><br/>👉 *Reaccioná a este mensaje para tomarlo.* |
| `group_order_assigned` | T07 (citando la publicación) | ✅ *{code}* asignado a *{courier_name}*. |
| `group_order_unassigned` | T10 / T16 (citando la publicación) | 🔓 *{code}* vuelve a estar disponible. Reaccioná de nuevo para tomarlo (si ya habías reaccionado, cambiá el emoji). |
| `group_order_cancelled` | T13 / T14 (citando la publicación) | ❌ *{code}* fue cancelado. No hace falta retirarlo. |
| `group_order_delivered` | T15 / T17, si `ANNOUNCE_DELIVERED_IN_GROUP` | 📦 *{code}* entregado por {courier_name}. |

## Mensajes al motero (privado)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `courier_order_assigned` | T07: ganó la toma | ✅ *Te asignamos el pedido {code}*<br/><br/>{order_details}<br/><br/>Cuando lo entregues, respondé este mensaje con *entregado*. Si no podés hacerlo, quitá tu reacción en el grupo. |
| `courier_order_already_taken` | T08 (una vez por motero y pedido) | El pedido *{code}* ya lo tomó otro motero. ¡Gracias igual! |
| `courier_order_released` | T10 / T16: liberó el pedido o lo liberó el administrador | Liberaste el pedido *{code}*; ya no está a tu nombre. (Si lo liberó la administración: "La administración liberó el pedido *{code}*; ya no está a tu nombre.") |
| `courier_order_cancelled` | T14: el pedido asignado se canceló | ❌ Se canceló el pedido *{code}*. Si ya lo retiraste, devolvelo en {pickup_address}. |
| `courier_order_closed_by_admin` | T17 | La administración cerró el pedido *{code}* como entregado. |
| `courier_order_delivered_ack` | T15 | 👍 Registré el pedido *{code}* como entregado. |
| `courier_delivered_ambiguous` | CU-09 2a | Tenés varios pedidos asignados: {active_codes}. Escribí *entregado* y el código, por ejemplo *entregado {code}*. |
| `courier_order_not_found` | CU-09 2b | No encontré un pedido asignado a vos con ese código. Tus pedidos asignados: {active_codes}. |
| `courier_delivery_reminder` | T18 | ⏰ ¿Pudiste entregar el pedido *{code}*? Si ya lo entregaste, respondé *entregado {code}*. |
| `courier_help` | CU-09: otro texto de un motero (con límite de frecuencia) | Para tomar pedidos, reaccioná a la publicación en el grupo. Para cerrar uno, escribime *entregado* y el código. |

> `courier_order_released` puede implementarse como dos claves (`courier_order_released` y
> `courier_order_released_by_admin`); se deja como una sola con variante para no duplicar el catálogo.

## Mensajes al administrador (privado a `ADMIN_PHONES`)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_order_unclaimed` | T12: `UNCLAIMED_ALERT_MINUTES` sin toma (una vez por período) | ⚠️ El pedido *{code}* lleva {minutes} minutos sin motero.<br/><br/>{order_details}<br/><br/>Sigue publicado. Si querés cancelarlo, escribí *cancelar {code}*. |
| `admin_delivery_overdue` | T18 | ⏰ El pedido *{code}* está asignado a {courier_name} hace más de {minutes} minutos y no se marcó como entregado. Para cerrarlo: *cerrar {code}*. |
| `admin_daily_report` | CU-14 (documento `.xlsx`; este es el pie) | 📊 Reporte de la jornada {report_date}: {orders_count} pedidos, {delivered_count} entregados, {couriers_count} moteros con actividad. |
| `admin_daily_report_failed` | CU-14: se agotaron los intentos | ❗ No pude generar o enviar el reporte del {report_date}. Error: {error}. |
| `admin_command_ok` | CU-13: comando ejecutado | ✅ Listo: *{code}* {action_label}. |
| `admin_command_error` | CU-13 2a | No pude {action_label} *{code}*: {reason}. |
| `admin_help` | CU-13: texto que no es comando | Comandos disponibles:<br/>*cancelar {código}*: cancela el pedido<br/>*liberar {código}*: lo saca del motero y lo vuelve a ofrecer<br/>*cerrar {código}*: lo marca como entregado |
| `admin_rate_limited_sender` | CU-15: un remitente superó el límite (una vez por día y remitente) | ℹ️ {sender_name} mandó demasiados mensajes seguidos; los estoy ignorando por un rato. |
| `admin_llm_failure` | CU-01 6a | ❗ No pude procesar un mensaje de {store_name}: el servicio de lectura de pedidos no responde. |
| `admin_outbound_failed` | Envío con `status = 'failed'` | ❗ No se pudo enviar el mensaje `{template_key}` a {recipient_name} tras {attempts} intentos. |
| `admin_whatsapp_disconnected` | Evento `connection.update` con estado cerrado | ❗ El número del bot se desconectó de WhatsApp. Hay que volver a vincularlo desde el manager. |

`{action_label}`: `cancelar` → "cancelado", `liberar` → "liberado", `cerrar` → "cerrado como entregado"
(en `admin_command_error` se usa el infinitivo). `admin_whatsapp_disconnected` no podrá llegar mientras
el número esté desconectado; se registra en logs y se envía al reconectar.

## Reglas de envío

- Los mensajes al grupo derivados de una publicación (`group_order_assigned`, `group_order_unassigned`,
  `group_order_cancelled`, `group_order_delivered`) se envían **citando** la publicación del pedido.
- `store_order_published` y `courier_order_assigned` guardan su `wa_message_id`, para que el local
  (*cancelar*) y el motero (*entregado*) puedan responder citándolos.
- Nunca se envía el mismo mensaje dos veces para el mismo evento: lo impide `idempotency_key`.
- `admin_daily_report` es de tipo `document`: archivo `reporte-ya-voy-AAAA-MM-DD.xlsx`, tipo MIME
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, enviado con `sendMedia`.
- Comandos reconocidos (sin distinguir mayúsculas ni tildes):
  - local → `cancelar [código]`, `ayuda`;
  - motero → `entregado [código]`, `ayuda`;
  - administrador → `cancelar {código}`, `liberar {código}`, `cerrar {código}`, `ayuda`.
