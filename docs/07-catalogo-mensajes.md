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
| `{status_label}` | Estado legible: `cancelled` → "cancelado", `expired` → "vencido" |
| `{hours_start}`, `{hours_end}` | `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END` |
| `{report_date}` | Fecha del reporte (`dd/mm/aaaa`) |
| `{reason_text}` | `" por {reason}"` si el cierre tiene motivo; vacío si no ("Hoy no tomamos pedidos por lluvia.") |
| `{override_summary}` | Resumen de un cambio de horario (ver "Resúmenes de horario") |
| `{pending_codes}` | Códigos de pedidos publicados sin tomar |
| `{window_start}`, `{window_end}` | Horas de un cierre `closed_window` (`HH:MM`) |

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
| `store_order_unassigned` | T10 / T15: el motero liberó el pedido o lo liberó el administrador | ⚠️ {courier_name} liberó tu pedido *{code}*. Ya está disponible de nuevo para los moteros. |
| `store_order_cancelled` | T13 / T14 por el local | ❌ Pedido *{code}* cancelado. |
| `store_order_cancelled_by_admin` | T13 / T14 por el administrador | ❌ La administración canceló tu pedido *{code}*. |
| `store_draft_cancelled` | T06: borrador descartado | Listo, descarté el pedido que estabas cargando. |
| `store_draft_expired` | T05: borrador vencido | ⌛ Descarté el pedido que estabas cargando porque pasaron {minutes} minutos sin datos nuevos. Si todavía lo necesitás, mandalo de nuevo completo. |
| `store_out_of_hours` | CU-09: fuera del horario base (con límite de frecuencia) | 🌙 Estamos fuera de horario. Tomamos pedidos de {hours_start} a {hours_end}. Mandalo de nuevo cuando abramos. |
| `store_closed_today` | CU-09: cierre `closed_today` activo | 🚫 Hoy no tomamos pedidos{reason_text}. |
| `store_closed_until_further_notice` | CU-09: cierre `closed_until_further_notice` activo | 🚫 Por el momento no estamos tomando pedidos{reason_text}. Probá de nuevo más tarde. |
| `store_closed_window` | CU-09: cierre `closed_window` activo | 🚫 Hoy de {window_start} a {window_end} no tomamos pedidos{reason_text}. Escribinos de nuevo a partir de las {window_end}. |
| `store_cancel_ambiguous` | CU-08 2a: `cancelar` sin código con varios pedidos activos | Tenés varios pedidos activos: {active_codes}. Escribí *cancelar* y el código, por ejemplo *cancelar {code}*. |
| `store_order_not_found` | CU-08 2b: código inexistente o ajeno | No encontré un pedido activo con ese código. Tus pedidos activos: {active_codes}. |
| `store_cancel_not_allowed` | CU-08 3a: pedido ya cancelado o vencido | El pedido *{code}* ya está {status_label} y no se puede cancelar. |
| `store_help` | CU-01 6b: mensaje sin datos de pedido (con límite de frecuencia) | Hola 👋 Para pedir un envío mandame:<br/>📍 Dirección de retiro<br/>📍 Dirección de entrega<br/>🥪 Producto<br/>💳 Cómo abona<br/><br/>Para cancelar: *cancelar* y el código del pedido. |
| `store_unsupported_media` | CU-01: audio, imagen sin texto, sticker | Por ahora solo puedo leer mensajes de texto. ¿Me lo mandás escrito? |
| `store_processing_error` | CU-01 6a: falla persistente del LLM | Tuve un problema para leer tu mensaje. Probá mandarlo de nuevo en unos minutos. |

## Mensajes al grupo de moteros

| Clave | Disparador | Texto |
|-------|------------|-------|
| `group_order_post` | T04: publicación (única) | 🆕 *Pedido {code}*<br/><br/>{order_details}<br/><br/>👉 *Reaccioná a este mensaje para tomarlo.* |
| `group_order_assigned` | T07 (citando la publicación) | ✅ *{code}* asignado a *{courier_name}*. |
| `group_order_unassigned` | T10 / T15 (citando la publicación) | 🔓 *{code}* vuelve a estar disponible. Reaccioná de nuevo para tomarlo (si ya habías reaccionado, cambiá el emoji). |
| `group_order_cancelled` | T13 / T14 (citando la publicación) | ❌ *{code}* fue cancelado. No hace falta retirarlo. |

## Mensajes al motero (privado)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `courier_order_assigned` | T07: ganó la toma | ✅ *Te asignamos el pedido {code}*<br/><br/>{order_details}<br/><br/>Si no podés hacerlo, quitá tu reacción en el grupo para liberarlo. |
| `courier_order_already_taken` | T08 (una vez por motero y pedido) | El pedido *{code}* ya lo tomó otro motero. ¡Gracias igual! |
| `courier_order_released` | T10: liberó el pedido | Liberaste el pedido *{code}*; ya no está a tu nombre. |
| `courier_order_released_by_admin` | T15: lo liberó el administrador | La administración liberó el pedido *{code}*; ya no está a tu nombre. |
| `courier_order_cancelled` | T14: el pedido asignado se canceló | ❌ Se canceló el pedido *{code}*. Si ya lo retiraste, devolvelo en {pickup_address}. |
| `courier_help` | CU-16: cualquier mensaje privado de un motero (con límite de frecuencia) | Para tomar un pedido, reaccioná a su publicación en el grupo. Si no podés hacerlo, quitá tu reacción para liberarlo. |

## Mensajes al administrador (privado a `ADMIN_PHONES`)

### Alertas y reporte

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_order_unclaimed` | T12: `UNCLAIMED_ALERT_MINUTES` sin toma (una vez por período) | ⚠️ El pedido *{code}* lleva {minutes} minutos sin motero.<br/><br/>{order_details}<br/><br/>Sigue publicado. Si querés cancelarlo, escribí *cancelar {code}*. |
| `admin_daily_report` | CU-15 (documento `.xlsx`; este es el pie) | 📊 Reporte del {report_date}: {orders_count} pedidos publicados, {assigned_count} tomados, {cancelled_count} cancelados, {couriers_count} moteros con actividad.{closures_note} |
| `admin_report_pending_reminder` | CU-15 3b: a `REPORT_PENDING_REMINDER_TIME` siguen pendientes (una vez) | ⏳ El reporte del {report_date} está listo, pero lo retengo porque quedan pedidos sin tomar: {pending_codes}. Cuando estén tomados o cancelados te lo mando. Para cancelar uno: *cancelar {code}*. |
| `admin_daily_report_failed` | CU-15: se agotaron los intentos | ❗ No pude generar o enviar el reporte del {report_date}. Error: {error}. |
| `admin_rate_limited_sender` | CU-16: un remitente superó el límite (una vez por día y remitente) | ℹ️ {sender_name} mandó demasiados mensajes seguidos; los estoy ignorando por un rato. |
| `admin_llm_failure` | CU-01 6a | ❗ No pude procesar un mensaje de {store_name}: el servicio de lectura de pedidos no responde. |
| `admin_outbound_failed` | Envío con `status = 'failed'` | ❗ No se pudo enviar el mensaje `{template_key}` a {recipient_name} tras {attempts} intentos. |
| `admin_whatsapp_disconnected` | Evento `connection.update` con estado cerrado | ❗ El número del bot se desconectó de WhatsApp. Hay que volver a vincularlo desde el manager. |

`{closures_note}`: si hubo cierres en el día, `"\nCierres del día: {lista}"`; si no, vacío.
`admin_whatsapp_disconnected` no podrá llegar mientras el número esté desconectado; se registra en logs y se
envía al reconectar.

### Comandos sobre pedidos

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_command_ok` | CU-12: comando ejecutado | ✅ Listo: *{code}* {action_label}. |
| `admin_command_error` | CU-12 2a | No pude {action_verb} *{code}*: {reason}. |
| `admin_help` | Texto que no es comando ni instrucción de horario | Esto es lo que puedo hacer:<br/>*cancelar {código}*: cancela el pedido<br/>*liberar {código}*: lo saca del motero y lo vuelve a ofrecer<br/>*estado*: horario y pedidos de hoy<br/><br/>También podés cambiar el horario de hoy escribiendo, por ejemplo:<br/>_hoy no tomamos pedidos por lluvia_<br/>_de 15 a 18 no tomamos pedidos_<br/>_hoy no tomamos pedidos hasta nuevo aviso_<br/>_ya tomamos pedidos de nuevo_ |

`{action_label}`: `cancelar` → "cancelado", `liberar` → "liberado". `{action_verb}`: "cancelar", "liberar".

### Horario del día

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_schedule_confirm` | CU-13 paso 5: instrucción interpretada y validada | Entendí esto:<br/>{override_summary}<br/><br/>¿Lo aplico? Respondé *sí* o *no* (tenés {minutes} minutos). |
| `admin_schedule_applied` | CU-13 paso 8: confirmado | ✅ Listo. {override_summary}<br/>{status_line} |
| `admin_schedule_rejected` | CU-13 6a: respondió "no" | Ok, no cambié nada. |
| `admin_schedule_confirmation_expired` | CU-13 6b: "sí" fuera de plazo | La confirmación venció y no cambié nada. Si querés, mandame la instrucción de nuevo. |
| `admin_schedule_invalid` | CU-13 2a: validación fallida | No pude aplicar eso: {reason}. Probá de nuevo, por ejemplo: _de 15 a 18 no tomamos pedidos_. |
| `admin_schedule_parse_error` | CU-13 1b: falla del LLM | No pude interpretar el mensaje en este momento. Probá de nuevo en unos minutos. |
| `admin_schedule_nothing_to_reopen` | CU-13 2b: `reopen` sin cierres en vigor | Ya se están tomando pedidos; no hay ningún cierre en vigor.{future_closures_note} |
| `admin_schedule_still_closed` | RF-37: inicio del horario base con cierre "hasta nuevo aviso" (una vez por día) | 🔒 Recordatorio: la toma de pedidos sigue cerrada hasta nuevo aviso{reason_text} (desde el {since}). Para reabrir, escribí _ya tomamos pedidos de nuevo_. |
| `admin_schedule_status` | CU-14: `estado` u `horario` | 📅 *Hoy {date}*<br/>Horario: {hours_start} a {hours_end}<br/>Cierres: {closures_list}<br/>Ahora: {accepting_label}<br/>Sin tomar: {pending_codes}<br/>Pedidos de hoy: {orders_count} publicados, {assigned_count} tomados, {cancelled_count} cancelados{pending_confirmation_note} |

Resúmenes de horario (`{override_summary}`), siempre con fecha y horas explícitas en formato 24 h:

| `kind` | Texto |
|--------|-------|
| `closed_today` | Hoy {date} no se toman más pedidos{reason_paren}. |
| `closed_until_further_notice` | Desde ahora no se toman pedidos hasta que me avises{reason_paren}. |
| `closed_window` | El {date} de {window_start} a {window_end} no se toman pedidos{reason_paren}. |
| `reopen` | Vuelvo a tomar pedidos ahora. Se levanta: {closures_list}.{future_closures_note} |

`{reason_paren}`: `" (motivo: {reason})"` o vacío. `{accepting_label}`: "tomando pedidos" o "cerrado ({motivo})".
`{future_closures_note}`: si quedan cierres futuros hoy, `" Sigue programado: {lista}."`; si no, vacío.
`{status_line}`: el mismo texto que `{accepting_label}`, precedido de "Ahora: ".

Respuestas de confirmación reconocidas (sin distinguir mayúsculas ni tildes): `sí`, `si`, `dale`, `ok`,
`confirmo` → confirmar; `no` → rechazar. Cualquier otro texto mientras hay una solicitud pendiente se trata
como una instrucción nueva (la anterior queda `superseded`).

## Reglas de envío

- Los mensajes al grupo derivados de una publicación (`group_order_assigned`, `group_order_unassigned`,
  `group_order_cancelled`) se envían **citando** la publicación del pedido.
- `store_order_published` guarda su `wa_message_id`, para que el local pueda responder *cancelar* citándolo.
- Nunca se envía el mismo mensaje dos veces para el mismo evento: lo impide `idempotency_key`.
- `admin_daily_report` es de tipo `document`: archivo `reporte-ya-voy-AAAA-MM-DD.xlsx`, tipo MIME
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, enviado con `sendMedia`.
- Los cierres de horario nunca generan mensajes a locales o moteros por iniciativa del bot; los locales se
  enteran solo cuando escriben (RNF-06).
- Comandos reconocidos (sin distinguir mayúsculas ni tildes):
  - local → `cancelar [código]`, `ayuda`;
  - motero → ninguno (cualquier texto recibe `courier_help`);
  - administrador → `cancelar {código}`, `liberar {código}`, `estado`, `horario`, `ayuda`, `sí`/`no`
    (confirmaciones); cualquier otro texto se interpreta como posible cambio de horario.
