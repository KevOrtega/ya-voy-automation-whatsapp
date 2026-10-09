# 01 — Especificación de requisitos

## 1. Propósito y alcance

Ya Voy automatiza la operación de un despachante de envíos que hoy coordina todo por WhatsApp:
los locales mandan pedidos, alguien los revisa y los reenvía a un grupo de moteros, y el motero
que lo toma avisa. El sistema reemplaza esa intermediación manual por un bot que:

1. Recibe pedidos por chat privado en un número de WhatsApp dedicado, dentro del horario de operación.
2. Extrae los datos del pedido y valida que estén completos.
3. Pide al local los datos faltantes, admitiendo que el pedido llegue en varios mensajes.
4. Publica el pedido completo en el grupo de moteros.
5. Asigna el pedido al primer motero que reacciona a la publicación, con garantía de un único ganador.
   Un pedido tomado se da por resuelto: no hay seguimiento de la entrega.
6. Notifica a cada parte (local, motero, grupo, administrador) en cada cambio relevante.
7. Permite al administrador cerrar o reabrir la toma de pedidos del día escribiendo en lenguaje natural.
8. Envía al administrador un reporte diario en Excel cuando todos los pedidos del día están resueltos.

**Fuera de alcance de la primera versión (v1):** seguimiento de retiro o entrega, cobro o liquidación a
moteros, cálculo de tarifas, seguimiento GPS, panel web, alta manual de locales o moteros, republicación
automática de pedidos, ampliación del horario base, integración con la API oficial de WhatsApp (Cloud API)
y procesamiento de audios o imágenes.

## 2. Actores

| Actor | Identificador | Descripción |
|-------|---------------|-------------|
| Local | `store` | Cualquier número que escribe al bot por privado y no es administrador ni motero conocido. Se da de alta automáticamente en su primer mensaje, con el nombre de perfil de WhatsApp (`pushName`). |
| Motero | `courier` | Participante del grupo de moteros. Se da de alta automáticamente la primera vez que reacciona a una publicación. Se identifica por su id de participante de WhatsApp (`@lid` o número). El administrador también puede tomar pedidos como motero. |
| Administrador | `admin` | El cliente (jefe de los moteros). Su número está en `ADMIN_PHONES`. Recibe alertas y el reporte diario, interviene pedidos con comandos y modifica el horario del día. |
| Sistema | `system` | El bot: API, worker y tareas programadas (vencimientos, alertas, reporte diario). |
| Servicios externos | — | Evolution API (pasarela de WhatsApp) y el proveedor de LLM. No son actores de negocio, pero aparecen en las secuencias. |

## 3. Requisitos funcionales

### Recepción y filtrado

| ID | Requisito |
|----|-----------|
| RF-01 | El sistema recibe los eventos de WhatsApp mediante un webhook de Evolution API. |
| RF-02 | El sistema ignora los mensajes propios del bot (`fromMe`), los de grupos distintos del grupo de moteros (`COURIERS_GROUP_JID`), los estados (`status@broadcast`) y, dentro del grupo de moteros, todo lo que no sea una reacción a una publicación de pedido. |
| RF-03 | Cada mensaje privado se clasifica según el remitente: administrador (número en `ADMIN_PHONES`), motero conocido (coincide con `couriers.phone_jid` o `couriers.lid`) o, en cualquier otro caso, local. |
| RF-04 | Si el remitente clasificado como local no existe, se crea automáticamente (`stores`) con su JID, su número cuando se puede resolver y su `pushName` como nombre. El nombre se actualiza si cambia. |
| RF-05 | El sistema aplica un límite de mensajes por remitente (`SENDER_RATE_LIMIT_PER_MINUTE`). Los mensajes que lo exceden se registran pero no se procesan. Un local o motero marcado como bloqueado (`is_blocked`) se ignora. |
| RF-06 | Los mensajes privados de un motero conocido nunca generan pedidos; se responden con `courier_help` (con límite de frecuencia). |

### Horario de operación

| ID | Requisito |
|----|-----------|
| RF-07 | El horario base es de `BUSINESS_HOURS_START` (09:00) a `BUSINESS_HOURS_END` (23:00) en la zona `TZ` (`America/Argentina/Buenos_Aires`). |
| RF-08 | El horario efectivo es el horario base menos los cierres activos cargados por el administrador (`schedule_overrides`). |
| RF-09 | Fuera del horario efectivo, los mensajes de un local que no sean comandos reciben una respuesta (como máximo una cada `OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES`) y no crean ni modifican borradores ni pedidos. Si el cierre se debe a un cierre del administrador, la respuesta indica el tipo de cierre y el motivo cuando se informó ("Hoy no tomamos pedidos por lluvia"). |
| RF-10 | Las reacciones, liberaciones, cancelaciones, comandos del administrador y tareas programadas funcionan a cualquier hora. |

### Carga y validación de pedidos

| ID | Requisito |
|----|-----------|
| RF-11 | Por cada mensaje de texto de un local, el sistema extrae los datos del pedido mediante un LLM a un esquema estructurado (`OrderExtraction`). |
| RF-12 | La validación de campos obligatorios se hace con código determinístico, no con el LLM. |
| RF-13 | Si el local no tiene un borrador abierto, el mensaje crea uno. Si tiene uno, los datos extraídos se combinan con el borrador (los valores nuevos no vacíos reemplazan a los anteriores). |
| RF-14 | El sistema espera un breve intervalo sin mensajes nuevos del local (`DRAFT_DEBOUNCE_SECONDS`) antes de evaluar el borrador y responder, para no contestar a cada mensaje de una ráfaga. |
| RF-15 | Si el borrador está incompleto, el sistema responde al local indicando exactamente qué campos faltan. |
| RF-16 | Si el borrador está completo, el sistema lo publica en el grupo de moteros (un único mensaje por pedido) y confirma al local con un código de pedido y un resumen. |
| RF-17 | Un borrador sin actividad durante `DRAFT_TTL_MINUTES` (30) vence; el sistema avisa al local. |
| RF-18 | El local puede descartar el borrador en curso con el comando `cancelar`. |

### Asignación (toma) y liberación

| ID | Requisito |
|----|-----------|
| RF-19 | Cualquier participante del grupo de moteros (incluido el administrador) que reacciona con cualquier emoji a una publicación de pedido es un motero. Si no existe, se crea automáticamente (`couriers`) con su id de participante (`@lid` o JID de número), su número cuando se puede resolver (`participantAlt`, `senderPn` u otros campos alternativos que entregue Evolution) y su `pushName`. Los datos se completan y el nombre se actualiza en eventos posteriores. |
| RF-20 | La asignación es atómica en la base de datos: si varios moteros reaccionan casi al mismo tiempo, gana exactamente uno. |
| RF-21 | Al ganador se le envía un mensaje privado con la confirmación y el detalle del pedido, sin datos de contacto del local. |
| RF-22 | Al local se le informa solo el nombre visible del motero ("Tu motero para este pedido será Emiliano"), sin su número. |
| RF-23 | Se responde en el grupo, citando la publicación, "Asignado a {motero}". |
| RF-24 | La reacción de un motero que no ganó no cambia la asignación; se le avisa por privado que el pedido ya fue tomado (`NOTIFY_LOSER_ON_RACE`, activo por defecto). |
| RF-25 | Si el motero asignado quita su reacción, el pedido se libera y vuelve a estar disponible; se notifica al local y al grupo. |
| RF-26 | Quitar una reacción solo libera el pedido si quien la quita es el motero asignado. |
| RF-27 | Cambiar de emoji no libera ni reasigna el pedido. |
| RF-28 | Un pedido asignado se considera resuelto y se cuenta como tomado. No hay confirmación de retiro ni de entrega. |

### Pedidos sin tomar y cancelación

| ID | Requisito |
|----|-----------|
| RF-29 | El sistema no republica pedidos. Si un pedido publicado no es tomado en `UNCLAIMED_ALERT_MINUTES` (20), se alerta una sola vez al administrador; el pedido sigue publicado y se puede tomar. Si el pedido se libera, el plazo vuelve a empezar y puede generar una nueva alerta. |
| RF-30 | El local puede cancelar un pedido publicado o asignado con `cancelar {código}` o respondiendo (citando) el mensaje de confirmación con `cancelar`. |
| RF-31 | Al cancelarse un pedido publicado, se avisa en el grupo; si estaba asignado, también al motero por privado. |

### Administración

| ID | Requisito |
|----|-----------|
| RF-32 | El administrador interviene pedidos escribiendo al bot por privado: `cancelar {código}` y `liberar {código}`. Cada intervención genera las mismas notificaciones que la acción equivalente del local o del motero. |
| RF-33 | El administrador consulta el estado del día con `estado` u `horario`: horario base, cierres activos y futuros, si se están tomando pedidos, pedidos sin tomar y conteos del día. |
| RF-34 | El administrador puede modificar el horario escribiendo en lenguaje natural ("hoy no tomamos pedidos por lluvia", "hoy no tomamos pedidos hasta nuevo aviso", "de 15 a 18 no tomaremos pedidos hoy", "ya tomamos pedidos de nuevo"). Un LLM interpreta el mensaje a un esquema estructurado (`ScheduleOverrideIntent`) con tipo `closed_today`, `closed_until_further_notice`, `closed_window` (fecha, inicio, fin) o `reopen`, y motivo opcional. La validación del resultado es determinística. |
| RF-35 | Antes de aplicar un cambio de horario, el bot responde con un resumen en español claro y pide confirmación (`sí` / `no`). Solo se aplica si el administrador confirma dentro de `SCHEDULE_CONFIRMATION_TTL_MINUTES` (5); si no, la solicitud vence sin efecto. |
| RF-36 | `reopen` levanta los cierres que están en vigor al momento de confirmar; los cierres futuros del día se mantienen y se informan en el resumen. |
| RF-37 | Si hay un cierre "hasta nuevo aviso" activo, al comenzar el horario base de cada día se le recuerda al administrador (`admin_schedule_still_closed`). |
| RF-38 | No hay alta, baja ni modificación manual de locales o moteros. El bloqueo (`is_blocked`) se hace directamente en la base en v1. |

### Reporte diario

| ID | Requisito |
|----|-----------|
| RF-39 | A partir de `DAILY_REPORT_TIME` (23:00 en `TZ`) el worker prepara el reporte del día: un archivo `.xlsx` que se envía como documento de WhatsApp a cada número de `ADMIN_PHONES`. |
| RF-40 | El reporte no se envía mientras haya pedidos del día pendientes (`published` sin tomar, o borradores aún abiertos). El worker vuelve a verificar periódicamente y lo envía en cuanto todos están tomados, cancelados o vencidos. |
| RF-41 | Si a `REPORT_PENDING_REMINDER_TIME` (01:00 del día siguiente) siguen quedando pedidos pendientes, se le recuerda una vez al administrador, con sus códigos, que los tome o cancele. |
| RF-42 | La hoja `Resumen` tiene una fila por motero con actividad en los pedidos del día: nombre, teléfono (si se conoce), cantidad de pedidos tomados, liberados y cancelados después de asignados. |
| RF-43 | La hoja `Pedidos` tiene una fila por pedido publicado del día: código, horas de creación y asignación (en hora de Buenos Aires), nombre del local, dirección de retiro, dirección de entrega, producto, medio de pago, nombre del motero y estado. |
| RF-44 | El reporte de una fecha se envía una sola vez por destinatario, aunque el worker se reinicie o la tarea se ejecute más de una vez. Si el worker estuvo caído, el reporte se procesa en cuanto vuelve. |

### Auditoría e idempotencia

| ID | Requisito |
|----|-----------|
| RF-45 | Todo cambio de estado de un pedido queda registrado en un log de auditoría (`order_events`). |
| RF-46 | Todo cambio de horario (solicitado, confirmado, rechazado, vencido o levantado) queda registrado en `schedule_overrides`, con el texto original del administrador. |
| RF-47 | Todos los mensajes entrantes y salientes quedan registrados como historial de conversación (`messages`). |
| RF-48 | Los eventos duplicados del webhook (mismo id de mensaje de WhatsApp) se procesan una sola vez. |
| RF-49 | Los mensajes salientes se envían a través de una bandeja de salida (`outbound_messages`) con clave de idempotencia, para no duplicar notificaciones ante reintentos. |

## 4. Requisitos no funcionales

| ID | Requisito |
|----|-----------|
| RNF-01 | **Latencia del webhook.** El endpoint responde en menos de 200 ms (p95): persiste el evento crudo, encola y devuelve `200`. Todo el procesamiento ocurre en el worker. |
| RNF-02 | **Latencia de negocio.** Desde que un motero reacciona hasta que recibe la confirmación por privado: < 5 s (p95), sin contar demoras de WhatsApp. |
| RNF-03 | **Idempotencia.** Reprocesar un mismo evento no produce efectos duplicados (ni cambios de estado ni mensajes). Se garantiza con restricciones únicas en Postgres. |
| RNF-04 | **Consistencia.** Postgres es la fuente de verdad. Redis solo guarda la cola de trabajos, locks y contadores efímeros; perder Redis no debe perder pedidos, borradores ni cambios de horario. |
| RNF-05 | **Concurrencia.** Los mensajes de un mismo local se procesan en serie (lock por local) para que la combinación de borradores sea consistente. |
| RNF-06 | **Riesgo de bloqueo del número.** Evolution API usa Baileys (cliente no oficial de WhatsApp Web). Se usa un número dedicado, nunca el personal del cliente; no se envían mensajes masivos ni se inician conversaciones con desconocidos; los envíos salientes se limitan (`OUTBOUND_MIN_INTERVAL_MS`). Los cierres de horario no generan avisos masivos a locales. |
| RNF-07 | **Disponibilidad.** Si Evolution o el LLM no responden, los trabajos se reintentan con backoff; el pedido no se pierde. Si el LLM falla de forma persistente, se avisa al local que reintente y al administrador. |
| RNF-08 | **Observabilidad.** Logs estructurados (JSON) con `order_id`, `store_id`, `courier_id` y `wa_message_id`; endpoint `/health`. |
| RNF-09 | **Privacidad.** No se comparten datos de contacto entre las partes: el local solo ve el nombre visible del motero; el motero no recibe teléfonos del local. Los teléfonos de moteros solo aparecen en el reporte al administrador. |
| RNF-10 | **Retención.** A los `DATA_RETENTION_DAYS` (90) días se anonimizan direcciones, notas, textos de mensajes, payloads crudos y archivos de reportes. Se conservan los códigos, estados, tiempos y conteos. |
| RNF-11 | **Seguridad.** El webhook exige un secreto compartido; los secretos se configuran solo por variables de entorno. Los comandos de administración y los cambios de horario solo se aceptan de números en `ADMIN_PHONES`. |
| RNF-12 | **Portabilidad.** Todos los servicios corren en contenedores Docker; desarrollo con Docker Compose y producción en Dokku sin cambios de código (solo configuración). |
| RNF-13 | **Proveedor de LLM intercambiable.** La extracción de pedidos y la interpretación de cambios de horario se hacen detrás de interfaces propias; cambiar de proveedor o modelo es configuración. |
| RNF-14 | **Idioma.** Código, identificadores y commits en inglés; mensajes al usuario y reporte en español, centralizados en el catálogo de mensajes. |
| RNF-15 | **Zona horaria.** Las fechas se guardan en UTC (`timestamptz`); el horario, los cierres, la fecha del reporte y las horas que se muestran usan `TZ`. |

## 5. Reglas de negocio

| ID | Regla |
|----|-------|
| RN-01 | Un pedido tiene a lo sumo un motero asignado en cada momento. |
| RN-02 | Gana la asignación el primer motero cuya reacción se procesa con éxito en la base (`UPDATE ... WHERE courier_id IS NULL`), no el primero en el orden de llegada del webhook. |
| RN-03 | Cualquier emoji cuenta como toma. |
| RN-04 | Solo el motero asignado puede liberar el pedido quitando su reacción. Quitar la reacción de un motero no asignado no tiene efecto. |
| RN-05 | Un cambio de emoji del motero asignado no cambia nada. Un cambio de emoji de otro motero se trata como un intento de toma (normalmente rechazado porque ya está asignado). |
| RN-06 | Un pedido solo puede liberarse mientras está `assigned`. Después de `cancelled`, quitar la reacción no tiene efecto. |
| RN-07 | Un pedido `assigned` se da por resuelto. Solo puede volver a `published` (liberación) o pasar a `cancelled`. |
| RN-08 | Solo el local dueño del pedido (o el administrador) puede cancelarlo, esté publicado o asignado. |
| RN-09 | Un local tiene como máximo un borrador abierto a la vez. |
| RN-10 | Todo remitente privado que no es administrador ni motero conocido es un local; no hay registro previo. Los mensajes privados de un motero conocido nunca son pedidos. |
| RN-11 | Todo participante del grupo de moteros que reacciona a una publicación es un motero, incluido el administrador; no hay registro previo. Su identidad es el id de participante (`lid` o `phone_jid`); el número es un dato complementario que puede faltar. |
| RN-12 | Solo se consideran reacciones sobre publicaciones de pedidos hechas por el bot en el grupo configurado. |
| RN-13 | No hay límite de pedidos asignados simultáneamente por motero. |
| RN-14 | Los campos obligatorios del pedido son `pickup_address`, `delivery_address`, `product` y `payment_method` (sección 6). `notes` es opcional. |
| RN-15 | Un pedido publicado no se puede modificar. Para corregirlo, el local lo cancela y lo envía de nuevo. Mientras tiene pedidos publicados o asignados, los mensajes nuevos del local abren un borrador nuevo. |
| RN-16 | No se comparten datos de contacto: al local se le informa solo el nombre visible del motero y al motero solo el detalle del pedido (nombre del local, direcciones, producto, medio de pago, notas). |
| RN-17 | Fuera del horario efectivo no se crean ni modifican borradores (regla estricta). Un borrador solo se publica si su último mensaje llegó dentro del horario efectivo; un dato faltante que llega fuera de horario recibe la respuesta de cierre y el borrador vence solo. |
| RN-18 | Cada pedido se publica una sola vez en el grupo. Un pedido sin tomar genera como máximo una alerta al administrador por período de publicación; la decisión (esperar, avisar a moteros, cancelar) es del administrador. |
| RN-19 | El reporte de la fecha D cubre los pedidos publicados cuya creación (`created_at`) cae en la fecha local D. Los conteos por motero son: tomados = pedidos del día asignados a él al generar el reporte; liberados = liberaciones de pedidos del día que lo tenían asignado; cancelados = pedidos del día cancelados mientras estaban asignados a él. |
| RN-20 | El reporte de una fecha se envía una sola vez a cada administrador y solo cuando no quedan pedidos pendientes de esa fecha. |
| RN-21 | Un cambio de horario solo se aplica después de la confirmación explícita del administrador. Cada administrador tiene como máximo una solicitud pendiente de confirmación; una instrucción nueva reemplaza a la anterior. |
| RN-22 | Los cierres solo restringen el horario base; nunca lo amplían. |
| RN-23 | `closed_today` cierra desde la confirmación hasta el fin del día local; `closed_until_further_notice` cierra desde la confirmación hasta un `reopen`; `closed_window` cierra entre las horas indicadas de la fecha indicada (hoy por defecto, nunca en el pasado). |
| RN-24 | Los cierres no afectan a pedidos ya publicados o asignados: se pueden seguir tomando, liberando y cancelando. |

## 6. Datos del pedido

Campos que debe producir la extracción (`OrderExtraction`) y que se validan antes de publicar:

| Campo | Identificador | Obligatorio | Validación determinística |
|-------|---------------|-------------|---------------------------|
| Dirección de retiro | `pickup_address` | Sí | Texto libre no vacío. No se toma por defecto de ningún dato del local. Sin geocodificación. |
| Dirección de entrega | `delivery_address` | Sí | Texto libre no vacío. Sin geocodificación. |
| Producto | `product` | Sí | Texto libre no vacío (por ejemplo, "almacen"). |
| Medio de pago | `payment_method` + `payment_method_raw` | Sí | `payment_method` normalizado a `cash`, `transfer`, `prepaid` u `other`; `payment_method_raw` conserva el texto original. Si el texto existe pero no corresponde a ninguno de los tres primeros, se usa `other`. |
| Observaciones | `notes` | No | Texto libre. |

No se piden ni se guardan nombre ni teléfono del cliente final, ni monto a cobrar.

Ejemplo canónico de mensaje de un local:

```
📍 Dirección de retiro: casa nogal almendros
📍 Dirección de entrega: el ceibo 2871
🥪 Producto/: almacen
💳Abona: transferencia
```

## 7. Supuestos

- S-01. Hay un único número de WhatsApp para el bot (una instancia de Evolution) y un único grupo de moteros.
- S-02. El número del bot es miembro del grupo de moteros con permiso para escribir.
- S-03. Los moteros pueden recibir mensajes privados del número del bot, aunque WhatsApp solo exponga su `@lid` (a verificar con la versión de Evolution que se fije).
- S-04. Los locales escriben en texto. Las imágenes con texto (caption) se procesan solo por su texto.
- S-05. Los pedidos son para entrega inmediata; no hay pedidos programados.
- S-06. El volumen es bajo a medio (decenas a pocos cientos de pedidos por día), compatible con un solo VPS.
- S-07. El administrador escribe al bot desde un número que figura en `ADMIN_PHONES` y que WhatsApp expone como número (no solo como `@lid`) en el chat privado.

## 8. Decisiones del cliente (preguntas resueltas)

| ID | Tema | Decisión |
|----|------|----------|
| DC-01 | Campos obligatorios | Dirección de retiro, dirección de entrega, producto y medio de pago. Notas opcionales. Sin datos del cliente final ni monto a cobrar. El retiro no toma por defecto la dirección del local. |
| DC-02 | Direcciones | Texto libre, sin geocodificación. |
| DC-03 | Datos personales | No se comparten. El local recibe solo el nombre visible del motero; el motero no recibe teléfonos. |
| DC-04 | Modificar pedidos publicados | No se permite; se cancela y se reenvía. |
| DC-05 | Límite por motero | Sin límite. |
| DC-06 | Audios e imágenes | Solo texto en v1. |
| DC-07 | Pedidos sin tomar | Sin republicación. A los 20 minutos se alerta una vez al administrador, que decide. El pedido sigue disponible. |
| DC-08 | Aviso al motero que perdió la carrera | Sí. |
| DC-09 | Alta de locales y moteros | Sin registro previo: alta automática de locales al escribir por privado y de moteros al reaccionar en el grupo, con su `pushName`. |
| DC-10 | Confirmación de retiro | No. |
| DC-11 | Retención de datos | 90 días; luego se anonimizan. |
| DC-12 | Pedidos asignados sin entregar | Reemplazada por DC-16 (no hay seguimiento de entrega). |
| DC-13 | Horario | De 09:00 a 23:00 (Buenos Aires). Fuera de horario se responde al local y no se crean pedidos. |
| DC-14 | Administrador | El cliente (jefe de los moteros); su número va en `ADMIN_PHONES`. |
| DC-15 | Reporte diario | Excel diario enviado por WhatsApp al administrador, con resumen por motero y detalle de pedidos (condiciones de envío en DC-17). |
| DC-16 | Seguimiento de entrega | No existe. Un pedido se da por resuelto y se cuenta cuando un motero lo toma. Se eliminan el estado `delivered`, el comando `entregado`, el recordatorio de entrega y el comando `cerrar`. |
| DC-17 | Envío del reporte | Se prepara a las 23:00 pero no se envía mientras haya pedidos del día sin tomar. Se envía cuando todos están tomados o cancelados. Si a la 01:00 siguen pendientes, se le recuerda al administrador con los códigos. |
| DC-18 | Administrador que reacciona en el grupo | Toma el pedido como cualquier motero. |
| DC-19 | Motero que escribe por privado | Sus mensajes privados son solo de motero; nunca son pedidos. |
| DC-20 | Dato faltante después de las 23:00 | Regla estricta: respuesta de fuera de horario y el borrador vence. |
| DC-21 | Alcance del reporte | Los pedidos creados en el día; los conteos se hacen al momento de la toma (ya no aplica la jornada de 23 a 23). |
| DC-22 | Comandos del administrador | `cancelar` y `liberar` (más `estado`/`horario` y los cambios de horario). |
| DC-23 | Cambios de horario del día | El administrador cierra o reabre en lenguaje natural; el bot interpreta, resume, pide confirmación y recién entonces aplica. La respuesta a los locales incluye el motivo. |

## 9. Preguntas abiertas

| ID | Pregunta | Propuesta por defecto |
|----|----------|------------------------|
| PA-01 | ¿Se quiere poder ampliar el horario del día (por ejemplo, "hoy tomamos hasta las 00")? | No en v1; los cierres solo restringen (RN-22). |
| PA-02 | Cuando el administrador cierra la toma de pedidos, ¿se avisa en el grupo de moteros? | No. |
| PA-03 | Si hay un cierre "hasta nuevo aviso", ¿se le recuerda al administrador cada mañana al abrir? | Sí, una vez al comenzar el horario base (RF-37). |
| PA-04 | ¿Se puede cancelar un pedido asignado de un día cuyo reporte ya se envió? (El reporte enviado no se corrige.) | Sí; queda registrado en la auditoría, pero no se reenvía el reporte. |
| PA-05 | Si a la 01:00 siguen pedidos pendientes y el recordatorio no alcanza, ¿se repite (por ejemplo, cada hora) o se envía el reporte igual en algún momento? | Un único recordatorio; el reporte espera sin límite. |
