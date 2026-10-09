# 07 — Catálogo de mensajes

Todos los mensajes que envía el bot. La clave (`template_key`) es la que usa el código y se guarda en
`outbound_messages.template_key`. Los textos usan formato de WhatsApp (`*negrita*`, `_cursiva_`) y
variables `{así}`. Son una primera propuesta para revisar con el cliente.

**Privacidad (RN-17):** ningún mensaje incluye teléfonos. Al local solo se le muestra el nombre visible
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
| `{window_start}`, `{window_end}` | Horas de un cierre `closed_window` o una apertura `open_window` (`HH:MM`; después de medianoche se agrega "del {date+1}") |
| `{effective_hours}` | Horario efectivo del día: base más aperturas menos cierres ("09:00 a 00:00") |
| `{revision}` | Número de versión del reporte (2 = primera corrección) |
| `{report_label}` | "El reporte" o, si es la espera de una corrección por apertura extra, "La corrección del reporte" |
| `{ready_label}` | Concuerda con `{report_label}`: "está listo" (El reporte) o "está lista" (La corrección del reporte) |
| `{report_pronoun}` | Concuerda con `{report_label}`: "lo" (El reporte) o "la" (La corrección del reporte) |
| `{changes_list}` | Cambios netos de una corrección, uno por línea (ver "Textos de cambios") |

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
| `store_order_confirm_request` | T04: borrador completo (también tras cada corrección, T05 → T04) | 📝 Revisá tu pedido:<br/><br/>{order_details}<br/><br/>¿Confirmás el pedido? Respondé *SÍ* para enviarlo o corregí lo que haga falta. |
| `store_order_published` | T06: el local confirmó y el pedido se publicó | ✅ Pedido *{code}* enviado a los moteros. Te aviso cuando uno lo tome.<br/><br/>Para cancelarlo, respondé este mensaje con *cancelar*. |
| `store_order_not_sent_closed` | T07: el local confirmó fuera del horario efectivo | 🚫 No envié tu pedido porque en este momento no estamos tomando pedidos{reason_text}. Mandalo de nuevo cuando volvamos a tomar pedidos. |
| `store_order_assigned` | T10: un motero tomó el pedido | 🏍️ Tu motero para el pedido *{code}* será *{courier_name}*. |
| `store_order_unassigned` | T13 / T18: el motero liberó el pedido o lo liberó el administrador | ⚠️ {courier_name} liberó tu pedido *{code}*. Ya está disponible de nuevo para los moteros. |
| `store_order_cancelled` | T16 / T17 a pedido del local | ❌ Pedido *{code}* cancelado. Ya lo saqué del grupo de moteros.{courier_note} |
| `store_order_cancelled_by_admin` | T16 / T17 a pedido del administrador | ❌ La administración canceló tu pedido *{code}*.{courier_note} |
| `store_draft_cancelled` | T08: el local respondió *no* o *cancelar* antes de confirmar | Listo, descarté el pedido; no se envió a los moteros. |
| `store_draft_expired` | T09: borrador o confirmación vencidos | ⌛ Descarté el pedido que estabas cargando porque pasaron {minutes} minutos sin respuesta; no se envió a los moteros. Si todavía lo necesitás, mandalo de nuevo completo. |
| `store_out_of_hours` | CU-10: fuera del horario efectivo, sin cierre del administrador (con límite de frecuencia) | 🌙 Estamos fuera de horario. Hoy tomamos pedidos de {effective_hours}. Mandalo de nuevo cuando abramos. |
| `store_closed_today` | CU-10: cierre `closed_today` activo | 🚫 Hoy no tomamos pedidos{reason_text}. |
| `store_closed_until_further_notice` | CU-10: cierre `closed_until_further_notice` activo | 🚫 Por el momento no estamos tomando pedidos{reason_text}. Probá de nuevo más tarde. |
| `store_closed_window` | CU-10: cierre `closed_window` activo | 🚫 Hoy de {window_start} a {window_end} no tomamos pedidos{reason_text}. Escribinos de nuevo a partir de las {window_end}. |
| `store_cancel_ambiguous` | CU-09 2a: `cancelar` sin código ni cita con varios pedidos activos | Tenés varios pedidos activos: {active_codes}. Respondé *cancelar* citando el mensaje del pedido o escribí *cancelar* y el código, por ejemplo *cancelar {code}*. |
| `store_order_not_found` | CU-09 2b: código inexistente o ajeno | No encontré un pedido activo con ese código. Tus pedidos activos: {active_codes}. |
| `store_cancel_not_allowed` | CU-09 3a: pedido ya cancelado o vencido | El pedido *{code}* ya está {status_label} y no se puede cancelar. |
| `store_help` | CU-01: mensaje sin datos de pedido (con límite de frecuencia) | Hola 👋 Para pedir un envío mandame:<br/>📍 Dirección de retiro<br/>📍 Dirección de entrega<br/>🥪 Producto<br/>💳 Cómo abona<br/><br/>Te voy a mostrar un resumen para que lo confirmes antes de enviarlo.<br/>Para cancelar un pedido: respondé su mensaje con *cancelar* o escribí *cancelar* y el código. |
| `store_unsupported_media` | CU-01: audio, imagen sin texto, sticker | Por ahora solo puedo leer mensajes de texto. ¿Me lo mandás escrito? |
| `store_processing_error` | CU-01: falla persistente del LLM | Tuve un problema para leer tu mensaje. Probá mandarlo de nuevo en unos minutos. |

`{courier_note}`: si el pedido estaba asignado, `"\nYa le avisé a {courier_name}."`; si no, vacío.
`{reason_text}` en `store_order_not_sent_closed`: el motivo del cierre activo si lo hay; fuera del horario
efectivo sin cierre, `" (horario de hoy: {effective_hours})"`.

## Mensajes al grupo de moteros

| Clave | Tipo | Disparador | Texto |
|-------|------|------------|-------|
| `group_order_post` | `text` | T06: publicación (única) | 🆕 *Pedido {code}*<br/><br/>{order_details}<br/><br/>👉 *Reaccioná a este mensaje para tomarlo.* |
| `group_order_assigned` | `text` | T10 (citando la publicación) | ✅ *{code}* asignado a *{courier_name}*. |
| `group_order_unassigned` | `text` | T13 / T18 (citando la publicación) | 🔓 *{code}* vuelve a estar disponible. Reaccioná de nuevo para tomarlo (si ya habías reaccionado, cambiá el emoji). |
| `group_order_delete` | `delete` | T16 / T17: eliminar para todos la publicación y cada respuesta del bot sobre el pedido | (sin texto; `target_wa_message_id` = mensaje a eliminar) |
| `group_order_cancelled_edit` | `edit` | CU-09 6a: falló `group_order_delete` | ❌ *CANCELADO* — Pedido {code}. No lo tomen. |
| `group_order_cancelled` | `text` | CU-09 6a: fallaron eliminar y editar (citando la publicación) | ❌ *{code}* fue *CANCELADO*. No lo tomen. |

Los cierres y aperturas de horario no se anuncian en el grupo (RF-46).

## Mensajes al motero (privado)

| Clave | Disparador | Texto |
|-------|------------|-------|
| `courier_order_assigned` | T10: ganó la toma | ✅ *Te asignamos el pedido {code}*<br/><br/>{order_details}<br/><br/>Si no podés hacerlo, quitá tu reacción en el grupo para liberarlo. |
| `courier_order_already_taken` | T11 (una vez por motero y pedido) | El pedido *{code}* ya lo tomó otro motero. ¡Gracias igual! |
| `courier_order_released` | T13: liberó el pedido | Liberaste el pedido *{code}*; ya no está a tu nombre. |
| `courier_order_released_by_admin` | T18: lo liberó el administrador | La administración liberó el pedido *{code}*; ya no está a tu nombre. |
| `courier_order_cancelled` | T17: el pedido que tenía asignado se canceló | ❌ Tu pedido *{code}* ha sido cancelado:<br/><br/>{order_details}<br/><br/>No hace falta que lo retires. Si ya lo retiraste, devolvelo en el local. |
| `courier_order_no_longer_available` | T19: reaccionó a un pedido cancelado (si `NOTIFY_REACTION_ON_CANCELLED`, una vez por motero y pedido) | El pedido *{code}* fue cancelado y ya no está disponible. |
| `courier_help` | CU-18: cualquier mensaje privado de un motero (con límite de frecuencia) | Para tomar un pedido, reaccioná a su publicación en el grupo. Si no podés hacerlo, quitá tu reacción para liberarlo. |

## Mensajes al administrador (privado a `ADMIN_PHONES`)

### Alertas y reporte

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_order_unclaimed` | T15: `UNCLAIMED_ALERT_MINUTES` sin toma (una vez por período) | ⚠️ El pedido *{code}* lleva {minutes} minutos sin motero.<br/><br/>{order_details}<br/><br/>Sigue publicado. Si querés cancelarlo, escribí *cancelar {code}*. |
| `admin_daily_report` | CU-16 (documento `.xlsx`; este es el pie) | 📊 Reporte del {report_date}: {orders_count} pedidos publicados, {assigned_count} tomados, {cancelled_count} cancelados, {couriers_count} moteros con actividad.{schedule_note} |
| `admin_daily_report_correction` | CU-17 (documento `.xlsx`; este es el pie) | 🛠️ *Corrección* del reporte del {report_date} (versión {revision}):<br/>{changes_list}<br/><br/>Totales actualizados: {orders_count} pedidos publicados, {assigned_count} tomados, {cancelled_count} cancelados. Este archivo reemplaza a la versión anterior. |
| `admin_report_pending_reminder` | CU-16 / CU-17: `REPORT_PENDING_REMINDER_MINUTES` después del cierre efectivo siguen pendientes (una vez por cierre efectivo: antes del primer envío y tras una apertura extra sobre un día ya reportado) | ⏳ {report_label} del {report_date} {ready_label}, pero {report_pronoun} retengo porque quedan pedidos sin tomar: {pending_codes}. Cuando estén tomados o cancelados te {report_pronoun} mando. Para cancelar uno: *cancelar {code}*. |
| `admin_daily_report_failed` | CU-16 / CU-17: se agotaron los intentos | ❗ No pude generar o enviar el reporte del {report_date} (versión {revision}). Error: {error}. |
| `admin_rate_limited_sender` | CU-18: un remitente superó el límite (una vez por día y remitente) | ℹ️ {sender_name} mandó demasiados mensajes seguidos; los estoy ignorando por un rato. |
| `admin_llm_failure` | CU-01: falla persistente del LLM | ❗ No pude procesar un mensaje de {store_name}: el servicio de lectura de pedidos no responde. |
| `admin_outbound_failed` | Envío con `status = 'failed'` (incluye eliminar o editar en el grupo) | ❗ No se pudo enviar el mensaje `{template_key}` a {recipient_name} tras {attempts_text}. |
| `admin_whatsapp_disconnected` | Evento `connection.update` con estado cerrado | ❗ El número del bot se desconectó de WhatsApp. Hay que volver a vincularlo desde el manager. |

`{schedule_note}`: si hubo cierres o aperturas extra en el día, `"\nCambios de horario: {lista}"`; si no, vacío.
`{recipient_name}` en `admin_outbound_failed`: `stores.display_name` o `couriers.display_name` del
destinatario; si es el grupo de moteros, `"el grupo de moteros"`; si es un administrador, `"un administrador"`;
si no se lo conoce, `"un destinatario sin nombre"`. Nunca un teléfono (RN-17).
`{attempts_text}` en `admin_outbound_failed`: `outbound_messages.attempts` con singular o plural según el número:
`"1 intento"` si es 1 (un error permanente no se reintenta) y `"N intentos"` en otro caso (`"3 intentos"`). Nunca
`"intento(s)"`.
`admin_whatsapp_disconnected` no podrá llegar mientras el número esté desconectado; se registra en logs y se
envía al reconectar.

Textos de cambios (`{changes_list}`), un renglón por pedido:

| Cambio | Texto |
|--------|-------|
| `added` | • Pedido agregado *{code}* (tomado por {to_courier}) — o "(cancelado)" si se canceló. |
| `courier_changed` | • El pedido *{code}* pasó de {from_courier} a {to_courier}. |
| `cancelled` | • El pedido *{code}* fue cancelado (estaba asignado a {from_courier}). |

Ejemplo: "🛠️ *Corrección* del reporte del 09/10/2026 (versión 2):<br/>• El pedido *YV-0123* pasó de Joel a Emiliano."

### Comandos sobre pedidos

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_command_ok` | CU-13: comando ejecutado | ✅ Listo: *{code}* {action_label}.{courier_note} |
| `admin_command_error` | CU-13: código inexistente o estado que no lo permite | No pude {action_verb} *{code}*: {reason}. |
| `admin_help` | Texto que no es comando ni instrucción de horario | Esto es lo que puedo hacer:<br/>*cancelar {código}*: cancela el pedido y lo saca del grupo<br/>*liberar {código}*: lo saca del motero y lo vuelve a ofrecer<br/>*estado*: horario y pedidos de hoy<br/><br/>También podés cambiar el horario escribiendo, por ejemplo:<br/>_hoy no tomamos pedidos por lluvia_<br/>_de 15 a 18 no tomamos pedidos_<br/>_hoy no tomamos pedidos hasta nuevo aviso_<br/>_hoy hasta las 00_<br/>_ya tomamos pedidos de nuevo_ |

`{action_label}`: `cancelar` → "cancelado y eliminado del grupo", `liberar` → "liberado". `{action_verb}`: "cancelar", "liberar".
`{courier_note}` en `admin_command_ok`: si se canceló un pedido asignado, `" Le avisé a {courier_name}."`; si no, vacío.

### Horario del día

| Clave | Disparador | Texto |
|-------|------------|-------|
| `admin_schedule_confirm` | CU-14: instrucción interpretada y validada | Entendí esto:<br/>{override_summary}<br/><br/>¿Lo aplico? Respondé *sí* o *no* (tenés {minutes} minutos). |
| `admin_schedule_applied` | CU-14: confirmado | ✅ Listo. {override_summary}<br/>{status_line} |
| `admin_schedule_rejected` | CU-14: respondió "no" | Ok, no cambié nada. |
| `admin_schedule_confirmation_expired` | CU-14: "sí" fuera de plazo | La confirmación venció y no cambié nada. Si querés, mandame la instrucción de nuevo. |
| `admin_schedule_invalid` | CU-14: validación fallida (incluye aperturas fuera de límites) | No pude aplicar eso: {reason}. Probá de nuevo, por ejemplo: _de 15 a 18 no tomamos pedidos_ o _hoy hasta las 00_. |
| `admin_schedule_parse_error` | CU-14: falla del LLM | No pude interpretar el mensaje en este momento. Probá de nuevo en unos minutos. |
| `admin_schedule_nothing_to_reopen` | CU-14: `reopen` sin cierres en vigor | Ya se están tomando pedidos; no hay ningún cierre en vigor.{future_closures_note} |
| `admin_schedule_still_closed` | RF-45: inicio del horario base con cierre "hasta nuevo aviso" (una vez por día) | 🔒 Recordatorio: la toma de pedidos sigue cerrada hasta nuevo aviso{reason_text} (desde el {since}). Para reabrir, escribí _ya tomamos pedidos de nuevo_. |
| `admin_schedule_status` | CU-15: `estado` u `horario` | 📅 *Hoy {date}*<br/>Horario: {effective_hours} (base {hours_start} a {hours_end})<br/>Aperturas extra: {openings_list}<br/>Cierres: {closures_list}<br/>Ahora: {accepting_label}<br/>Sin tomar: {pending_codes}<br/>Pedidos de hoy: {orders_count} publicados, {assigned_count} tomados, {cancelled_count} cancelados<br/>Reporte: {report_status_label}{pending_confirmation_note} |

Valores de `{reason}` en `admin_schedule_invalid` (ejemplos): "la apertura tiene que quedar entre las 06:00 y
las 02:00 del día siguiente", "ese horario ya está dentro del horario normal", "la hora de fin tiene que ser posterior a la de inicio".
`{report_status_label}`: "se genera a las {due_time}", "retenido (pendientes: {codes})", "enviado",
"enviado, corrección pendiente" o "reabierto por apertura extra, corrección a las {due_time}".

Resúmenes de horario (`{override_summary}`), siempre con fecha y horas explícitas en formato 24 h:

| `kind` | Texto |
|--------|-------|
| `closed_today` | Hoy {date} no se toman más pedidos{reason_paren}. |
| `closed_until_further_notice` | Desde ahora no se toman pedidos hasta que me avises{reason_paren}. |
| `closed_window` | El {date} de {window_start} a {window_end} no se toman pedidos{reason_paren}. |
| `open_window` | El {date} se toman pedidos también de {window_start} a {window_end}{reason_paren}. Horario de ese día: {effective_hours}. {report_note} |
| `reopen` | Vuelvo a tomar pedidos ahora. Se levanta: {closures_list}.{future_closures_note} |

Ejemplo de `open_window` para "hoy hasta las 00" un 09/10: "El 09/10/2026 se toman pedidos también de 23:00 a
00:00 (del 10/10). Horario de ese día: 09:00 a 00:00. El reporte del día se genera a las 00:00."

`{report_note}`: si el reporte del día
todavía no se generó y la apertura termina después del cierre efectivo vigente, "El reporte del día se genera a las
{window_end}."; si el reporte ya se envió, "El reporte de ese día ya se envió: los pedidos nuevos irán en una
corrección a las {window_end}."; si no, vacío.

`{reason_paren}`: `" (motivo: {reason})"` o vacío. `{accepting_label}`: "tomando pedidos" o "cerrado ({motivo})".
`{future_closures_note}`: si quedan cierres futuros hoy, `" Sigue programado: {lista}."`; si no, vacío.
`{status_line}`: el mismo texto que `{accepting_label}`, precedido de "Ahora: ".

Respuestas de confirmación reconocidas (sin distinguir mayúsculas ni tildes), iguales para el local y el
administrador: `sí`, `si`, `dale`, `ok`, `confirmo` (y 👍 para el local) → confirmar; `no` → rechazar.
Para el administrador, cualquier otro texto mientras hay una solicitud pendiente se trata como una instrucción
nueva (la anterior queda `superseded`). Para el local, cualquier otro texto mientras espera confirmación se toma
como corrección del pedido (T05).

### Propuestas de la etapa 7a (pendientes de aprobación)

Textos que el código necesita y que este catálogo todavía no define. Se usan así hasta que el cliente los
apruebe o los cambie:

| Uso | Texto propuesto |
|-----|-----------------|
| `{reason}` de `admin_schedule_invalid`: falta la hora de inicio o la de fin | me faltan la hora de inicio y la de fin |
| `{reason}`: fecha pasada | esa fecha ya pasó |
| `{reason}`: la ventana ya terminó | ese horario ya terminó |
| `{reason}`: el cierre no quita horario efectivo (fuera de horario o dentro de otro cierre) | en ese horario ya no se toman pedidos |
| `{reason}`: apertura fuera de límites | el ejemplo de arriba, con los valores configurados de `SCHEDULE_EARLIEST_OPEN_TIME` y `SCHEDULE_LATEST_CLOSE_TIME` |
| Elementos de `{closures_list}` (separados por ", ") | `closed_today`: "cierre de hoy{reason_text}"; `closed_until_further_notice`: "cierre hasta nuevo aviso{reason_text}"; `closed_window`: "cierre de {window_start} a {window_end}{reason_text}" |
| `{effective_hours}` con varios tramos | "09:00 a 15:00 y 18:00 a 23:00" (", " entre tramos y " y " antes del último) |
| `{effective_hours}` sin horario | cerrado todo el día |
| `{accepting_label}` fuera de horario | cerrado (fuera de horario) |
| `{accepting_label}` con un cierre sin motivo | cerrado |
| `{date}` en los resúmenes y `{since}` | `dd/mm/aaaa`, como en el ejemplo de `open_window` |
| Al final de `{override_summary}` cuando la instrucción nueva reemplaza otras en la superposición (RN-25: gana la más reciente) | Reemplaza, en el horario que se superpone: {items}. |
| Elemento de `{items}` para una apertura (los cierres usan los elementos de `{closures_list}`) | apertura del {date} de {window_start} a {window_end}{reason_text} |
| Ejemplo completo (apertura después de un cierre por lluvia) | El 09/10/2026 se toman pedidos también de 19:00 a 23:00. Horario de ese día: 09:00 a 10:00 y 19:00 a 23:00. Reemplaza, en el horario que se superpone: cierre de hoy por lluvia. |

### Propuestas de la etapa 8a (pendientes de aprobación)

Textos del reporte diario que el código necesita y que este catálogo todavía no define. Se usan así hasta que
el cliente los apruebe o los cambie:

| Uso | Texto propuesto |
|-----|-----------------|
| Columna "Cambio" de `Pedidos` y "Detalle" breve, por tipo | `added`: "Agregado"; `courier_changed`: "Cambió de motero"; `cancelled`: "Cancelado" |
| Columna "Antes" de `Correcciones` para un pedido agregado | No estaba |
| Columnas "Antes"/"Ahora" de `Correcciones` cuando el pedido está asignado | Tomado por {courier} |
| Columna "Detalle" de `Correcciones` | La línea de "Textos de cambios" sin la viñeta ni los asteriscos (p. ej. "El pedido YV-0123 pasó de Joel a Emiliano.") |
| `{error}` de `admin_daily_report_failed` cuando falla el armado del archivo (el error técnico no se muestra; queda en `last_error`) | no pude armar el archivo |
| `{error}` de `admin_daily_report_failed` cuando falla el envío del documento | no se pudo enviar el archivo a {recipient_name} tras {attempts_text} |
| `{code}` de `admin_report_pending_reminder` ("Para cancelar uno: *cancelar {code}*") | El primer código de `{pending_codes}` |
| Elementos de `{schedule_note}` (separados por ", ", ordenados por hora de inicio) | Los mismos de la etapa 7a: "cierre de hoy{reason_text}", "cierre de {window_start} a {window_end}{reason_text}", "apertura del {date} de {window_start} a {window_end}{reason_text}" |
| Fechas y horas del archivo | Hora de Buenos Aires, formato `dd/mm/aaaa hh:mm` |

## Reglas de envío

- Los mensajes al grupo derivados de una publicación (`group_order_assigned`, `group_order_unassigned`,
  `group_order_cancelled`) se envían **citando** la publicación del pedido.
- `store_order_confirm_request` y `store_order_published` guardan su `wa_message_id`, para que el local pueda
  responder *sí* o *cancelar* citándolos.
- `group_order_delete` y `group_order_cancelled_edit` solo operan sobre mensajes propios del bot
  (`target_wa_message_id`); se usan `deleteMessageForEveryone` y `updateMessage` de Evolution.
- Los envíos a un mismo destinatario salen en el orden en que se encolaron (FIFO): un motero que gana la
  carrera contra una cancelación recibe primero `courier_order_assigned` y después `courier_order_cancelled`.
- Nunca se envía el mismo mensaje dos veces para el mismo evento: lo impide `idempotency_key`
  (`daily_report:{fecha}:v{revision}:{teléfono}` para cada versión del reporte).
- `admin_daily_report` y `admin_daily_report_correction` son de tipo `document`: archivos
  `reporte-ya-voy-AAAA-MM-DD.xlsx` y `reporte-ya-voy-AAAA-MM-DD-correccion-N.xlsx`, tipo MIME
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, enviados con `sendMedia`.
- Los cambios de horario (cierres y aperturas) nunca generan mensajes a locales o moteros por iniciativa del
  bot; los locales se enteran solo cuando escriben.
- Comandos reconocidos (sin distinguir mayúsculas ni tildes):
  - local → `sí`/`no` (mientras un pedido espera confirmación), `cancelar [código]` (o citando el mensaje del
    pedido), `ayuda`;
  - motero → ninguno (cualquier texto recibe `courier_help`);
  - administrador → `cancelar {código}`, `liberar {código}`, `estado`, `horario`, `ayuda`, `sí`/`no`
    (confirmaciones); cualquier otro texto se interpreta como posible cambio de horario.
