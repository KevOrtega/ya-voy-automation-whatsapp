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
6. Notifica a cada parte (local, motero, grupo, administrador) en cada cambio relevante.
7. Envía al administrador un reporte diario en Excel con la actividad de la jornada.

**Fuera de alcance de la primera versión (v1):** cobro o liquidación a moteros, cálculo de tarifas,
seguimiento GPS, panel web, alta manual de locales o moteros, republicación automática de pedidos,
integración con la API oficial de WhatsApp (Cloud API) y procesamiento de audios o imágenes.

## 2. Actores

| Actor | Identificador | Descripción |
|-------|---------------|-------------|
| Local | `store` | Cualquier número que escribe al bot por privado y no es administrador ni motero conocido. Se da de alta automáticamente en su primer mensaje, con el nombre de perfil de WhatsApp (`pushName`). |
| Motero | `courier` | Participante del grupo de moteros. Se da de alta automáticamente la primera vez que reacciona a una publicación. Se identifica por su id de participante de WhatsApp (`@lid` o número). |
| Administrador | `admin` | El cliente (jefe de los moteros). Su número está en `ADMIN_PHONES`. Recibe alertas y el reporte diario, y puede intervenir pedidos con comandos por WhatsApp. |
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

### Horario de operación

| ID | Requisito |
|----|-----------|
| RF-06 | El horario de operación es de `BUSINESS_HOURS_START` (09:00) a `BUSINESS_HOURS_END` (23:00) en la zona `TZ` (`America/Argentina/Buenos_Aires`). |
| RF-07 | Fuera de horario, los mensajes de un local que no sean comandos reciben `store_out_of_hours` (como máximo uno cada `OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES`) y no crean ni modifican borradores ni pedidos. |
| RF-08 | Las reacciones, liberaciones, entregas, cancelaciones, comandos del administrador y tareas programadas funcionan a cualquier hora. |

### Carga y validación de pedidos

| ID | Requisito |
|----|-----------|
| RF-09 | Por cada mensaje de texto de un local, el sistema extrae los datos del pedido mediante un LLM a un esquema estructurado (`OrderExtraction`). |
| RF-10 | La validación de campos obligatorios se hace con código determinístico, no con el LLM. |
| RF-11 | Si el local no tiene un borrador abierto, el mensaje crea uno. Si tiene uno, los datos extraídos se combinan con el borrador (los valores nuevos no vacíos reemplazan a los anteriores). |
| RF-12 | El sistema espera un breve intervalo sin mensajes nuevos del local (`DRAFT_DEBOUNCE_SECONDS`) antes de evaluar el borrador y responder, para no contestar a cada mensaje de una ráfaga. |
| RF-13 | Si el borrador está incompleto, el sistema responde al local indicando exactamente qué campos faltan. |
| RF-14 | Si el borrador está completo, el sistema lo publica en el grupo de moteros (un único mensaje por pedido) y confirma al local con un código de pedido y un resumen. |
| RF-15 | Un borrador sin actividad durante `DRAFT_TTL_MINUTES` (30) vence; el sistema avisa al local. |
| RF-16 | El local puede descartar el borrador en curso con el comando `cancelar`. |

### Asignación (toma) y liberación

| ID | Requisito |
|----|-----------|
| RF-17 | Cualquier participante del grupo de moteros que reacciona con cualquier emoji a una publicación de pedido es un motero. Si no existe, se crea automáticamente (`couriers`) con su id de participante (`@lid` o JID de número), su número cuando se puede resolver (`participantAlt`, `senderPn` u otros campos alternativos que entregue Evolution) y su `pushName`. Los datos se completan y el nombre se actualiza en eventos posteriores. |
| RF-18 | La asignación es atómica en la base de datos: si varios moteros reaccionan casi al mismo tiempo, gana exactamente uno. |
| RF-19 | Al ganador se le envía un mensaje privado con la confirmación y el detalle del pedido, sin datos de contacto del local. |
| RF-20 | Al local se le informa solo el nombre visible del motero ("Tu motero para este pedido será Emiliano"), sin su número. |
| RF-21 | Se responde en el grupo, citando la publicación, "Asignado a {motero}". |
| RF-22 | La reacción de un motero que no ganó no cambia la asignación; se le avisa por privado que el pedido ya fue tomado (`NOTIFY_LOSER_ON_RACE`, activo por defecto). |
| RF-23 | Si el motero asignado quita su reacción, el pedido se libera y vuelve a estar disponible; se notifica al local y al grupo. |
| RF-24 | Quitar una reacción solo libera el pedido si quien la quita es el motero asignado. |
| RF-25 | Cambiar de emoji no libera ni reasigna el pedido. |

### Pedidos sin tomar, cancelación y entrega

| ID | Requisito |
|----|-----------|
| RF-26 | El sistema no republica pedidos. Si un pedido publicado no es tomado en `UNCLAIMED_ALERT_MINUTES` (20), se alerta una sola vez al administrador; el pedido sigue publicado y se puede tomar. Si el pedido se libera, el plazo vuelve a empezar y puede generar una nueva alerta. |
| RF-27 | El local puede cancelar un pedido publicado o asignado con `cancelar {código}` o respondiendo (citando) el mensaje de confirmación con `cancelar`. |
| RF-28 | Al cancelarse un pedido publicado, se avisa en el grupo; si estaba asignado, también al motero por privado. |
| RF-29 | El motero asignado marca el pedido como entregado enviando por privado `entregado {código}`, o `entregado` citando el mensaje de asignación, o solo `entregado` si tiene un único pedido asignado. |
| RF-30 | Al marcarse como entregado, se notifica al local y se confirma al motero. |
| RF-31 | Si un pedido asignado no se marca como entregado en `DELIVERY_REMINDER_MINUTES` (90), se envía un recordatorio al motero y una alerta al administrador, una sola vez. El cierre posterior es manual. |

### Administración

| ID | Requisito |
|----|-----------|
| RF-32 | El administrador interviene pedidos por WhatsApp, escribiendo al bot por privado: `cancelar {código}`, `liberar {código}` y `cerrar {código}` (cierre manual como entregado). Cada intervención genera las mismas notificaciones que la acción equivalente del local o del motero. |
| RF-33 | No hay alta, baja ni modificación manual de locales o moteros. El bloqueo (`is_blocked`) se hace directamente en la base en v1. |

### Reporte diario

| ID | Requisito |
|----|-----------|
| RF-34 | Al cierre de la jornada (`DAILY_REPORT_TIME`, 23:00 en `TZ`) el worker genera un archivo `.xlsx` y lo envía como documento de WhatsApp a cada número de `ADMIN_PHONES`. |
| RF-35 | La hoja `Resumen` tiene una fila por motero con actividad en la jornada: nombre, teléfono (si se conoce), cantidad de pedidos asignados, entregados, liberados y cancelados. |
| RF-36 | La hoja `Pedidos` tiene una fila por pedido publicado en la jornada: código, horas de creación, asignación y entrega (en hora de Buenos Aires), nombre del local, dirección de retiro, dirección de entrega, producto, medio de pago, nombre del motero y estado. |
| RF-37 | El reporte de una fecha se envía una sola vez por destinatario, aunque el worker se reinicie o la tarea se ejecute más de una vez. Si el worker estuvo caído a la hora del reporte, el reporte se genera y envía en cuanto vuelve. |

### Auditoría e idempotencia

| ID | Requisito |
|----|-----------|
| RF-38 | Todo cambio de estado de un pedido queda registrado en un log de auditoría (`order_events`). |
| RF-39 | Todos los mensajes entrantes y salientes quedan registrados como historial de conversación (`messages`). |
| RF-40 | Los eventos duplicados del webhook (mismo id de mensaje de WhatsApp) se procesan una sola vez. |
| RF-41 | Los mensajes salientes se envían a través de una bandeja de salida (`outbound_messages`) con clave de idempotencia, para no duplicar notificaciones ante reintentos. |

## 4. Requisitos no funcionales

| ID | Requisito |
|----|-----------|
| RNF-01 | **Latencia del webhook.** El endpoint responde en menos de 200 ms (p95): persiste el evento crudo, encola y devuelve `200`. Todo el procesamiento ocurre en el worker. |
| RNF-02 | **Latencia de negocio.** Desde que un motero reacciona hasta que recibe la confirmación por privado: < 5 s (p95), sin contar demoras de WhatsApp. |
| RNF-03 | **Idempotencia.** Reprocesar un mismo evento no produce efectos duplicados (ni cambios de estado ni mensajes). Se garantiza con restricciones únicas en Postgres. |
| RNF-04 | **Consistencia.** Postgres es la fuente de verdad. Redis solo guarda la cola de trabajos, locks y contadores efímeros; perder Redis no debe perder pedidos ni borradores. |
| RNF-05 | **Concurrencia.** Los mensajes de un mismo local se procesan en serie (lock por local) para que la combinación de borradores sea consistente. |
| RNF-06 | **Riesgo de bloqueo del número.** Evolution API usa Baileys (cliente no oficial de WhatsApp Web). Se usa un número dedicado, nunca el personal del cliente; no se envían mensajes masivos ni se inician conversaciones con desconocidos; los envíos salientes se limitan (`OUTBOUND_MIN_INTERVAL_MS`). |
| RNF-07 | **Disponibilidad.** Si Evolution o el LLM no responden, los trabajos se reintentan con backoff; el pedido no se pierde. Si el LLM falla de forma persistente, se avisa al local que reintente y al administrador. |
| RNF-08 | **Observabilidad.** Logs estructurados (JSON) con `order_id`, `store_id`, `courier_id` y `wa_message_id`; endpoint `/health`. |
| RNF-09 | **Privacidad.** No se comparten datos de contacto entre las partes: el local solo ve el nombre visible del motero; el motero no recibe teléfonos del local ni del cliente final. Los teléfonos de moteros solo aparecen en el reporte al administrador. |
| RNF-10 | **Retención.** A los `DATA_RETENTION_DAYS` (90) días se anonimizan direcciones, notas, textos de mensajes, payloads crudos y archivos de reportes. Se conservan los códigos, estados, tiempos y conteos. |
| RNF-11 | **Seguridad.** El webhook exige un secreto compartido; los secretos se configuran solo por variables de entorno. Los comandos de administración solo se aceptan de números en `ADMIN_PHONES`. |
| RNF-12 | **Portabilidad.** Todos los servicios corren en contenedores Docker; desarrollo con Docker Compose y producción en Dokku sin cambios de código (solo configuración). |
| RNF-13 | **Proveedor de LLM intercambiable.** La extracción se hace detrás de una interfaz propia; cambiar de proveedor o modelo es configuración. |
| RNF-14 | **Idioma.** Código, identificadores y commits en inglés; mensajes al usuario y reporte en español, centralizados en el catálogo de mensajes. |
| RNF-15 | **Zona horaria.** Las fechas se guardan en UTC (`timestamptz`); el horario de operación, la hora del reporte y las horas que se muestran usan `TZ`. |

## 5. Reglas de negocio

| ID | Regla |
|----|-------|
| RN-01 | Un pedido tiene a lo sumo un motero asignado en cada momento. |
| RN-02 | Gana la asignación el primer motero cuya reacción se procesa con éxito en la base (`UPDATE ... WHERE courier_id IS NULL`), no el primero en el orden de llegada del webhook. |
| RN-03 | Cualquier emoji cuenta como toma. |
| RN-04 | Solo el motero asignado puede liberar el pedido quitando su reacción. Quitar la reacción de un motero no asignado no tiene efecto. |
| RN-05 | Un cambio de emoji del motero asignado no cambia nada. Un cambio de emoji de otro motero se trata como un intento de toma (normalmente rechazado porque ya está asignado). |
| RN-06 | Un pedido solo puede liberarse mientras está `assigned`. Después de `delivered` o `cancelled`, quitar la reacción no tiene efecto. |
| RN-07 | Solo el motero asignado (o el administrador con `cerrar`) puede marcar el pedido como entregado. |
| RN-08 | Solo el local dueño del pedido (o el administrador) puede cancelarlo. Un pedido `delivered` no se puede cancelar. |
| RN-09 | Un local tiene como máximo un borrador abierto a la vez. |
| RN-10 | Todo remitente privado que no es administrador ni motero conocido es un local; no hay registro previo. |
| RN-11 | Todo participante del grupo de moteros que reacciona a una publicación es un motero; no hay registro previo. Su identidad es el id de participante (`lid` o `phone_jid`); el número es un dato complementario que puede faltar. |
| RN-12 | Solo se consideran reacciones sobre publicaciones de pedidos hechas por el bot en el grupo configurado. |
| RN-13 | No hay límite de pedidos asignados simultáneamente por motero. |
| RN-14 | Los campos obligatorios del pedido son `pickup_address`, `delivery_address`, `product` y `payment_method` (sección 6). `notes` es opcional. |
| RN-15 | Un pedido publicado no se puede modificar. Para corregirlo, el local lo cancela y lo envía de nuevo. Mientras tiene pedidos publicados o asignados, los mensajes nuevos del local abren un borrador nuevo. |
| RN-16 | No se comparten datos de contacto: al local se le informa solo el nombre visible del motero y al motero solo el detalle del pedido (nombre del local, direcciones, producto, medio de pago, notas). |
| RN-17 | Fuera del horario de operación no se crean ni modifican borradores. |
| RN-18 | Cada pedido se publica una sola vez en el grupo. Un pedido sin tomar genera como máximo una alerta al administrador por período de publicación; la decisión (esperar, avisar a moteros, cancelar) es del administrador. |
| RN-19 | La jornada del reporte diario va desde el `DAILY_REPORT_TIME` del día anterior hasta el `DAILY_REPORT_TIME` del día del reporte. El resumen cuenta eventos ocurridos en ese intervalo; el detalle lista los pedidos publicados en ese intervalo con su estado al momento de generar el reporte. |
| RN-20 | Cada reporte diario (fecha) se envía una sola vez a cada administrador. |

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
| DC-01 | Campos obligatorios | Dirección de retiro, dirección de entrega, producto y medio de pago. Notas opcionales. Se eliminan nombre y teléfono del cliente y monto a cobrar. El retiro no toma por defecto la dirección del local. |
| DC-02 | Direcciones | Texto libre, sin geocodificación. |
| DC-03 | Datos personales | No se comparten. El local recibe solo el nombre visible del motero; el motero no recibe teléfonos. |
| DC-04 | Modificar pedidos publicados | No se permite; se cancela y se reenvía. |
| DC-05 | Límite por motero | Sin límite. |
| DC-06 | Audios e imágenes | Solo texto en v1. |
| DC-07 | Pedidos sin tomar | Sin republicación. A los 20 minutos se alerta una vez al administrador, que decide. El pedido sigue disponible. |
| DC-08 | Aviso al motero que perdió la carrera | Sí. |
| DC-09 | Alta de locales y moteros | Sin registro previo: alta automática de locales al escribir por privado y de moteros al reaccionar en el grupo, con su `pushName`. |
| DC-10 | Confirmación de retiro | No; solo entrega. |
| DC-11 | Retención de datos | 90 días; luego se anonimizan. |
| DC-12 | Pedidos asignados sin entregar | Recordatorio al motero a los 90 minutos, alerta al administrador y cierre manual. |
| DC-13 | Horario | De 09:00 a 23:00 (Buenos Aires). Fuera de horario se responde al local y no se crean pedidos. |
| DC-14 | Administrador | El cliente (jefe de los moteros); su número va en `ADMIN_PHONES`. |
| DC-15 | Reporte diario | Excel diario a las 23:00 enviado por WhatsApp al administrador, con resumen por motero y detalle de pedidos. |

## 9. Preguntas abiertas

| ID | Pregunta | Propuesta por defecto |
|----|----------|------------------------|
| PA-01 | Si el administrador (que está en el grupo) reacciona a una publicación, ¿toma el pedido como un motero más? | Sí, se lo trata como motero. |
| PA-02 | Un motero conocido que escribe por privado se trata como motero (comandos `entregado`). ¿Un motero puede también cargar pedidos como local? | No en v1. |
| PA-03 | Un local con un borrador abierto a las 22:58 que manda el dato faltante a las 23:01: ¿se acepta para completar el pedido o se responde "fuera de horario"? | Se responde fuera de horario (regla estricta); el borrador vence solo. |
| PA-04 | Las entregas después de las 23:00 cuentan en el resumen de la jornada siguiente (RN-19). ¿Le sirve así, o prefiere el reporte más tarde o por fecha calendario? | Jornada de 23:00 a 23:00. |
| PA-05 | ¿Los comandos por WhatsApp (`cancelar`, `liberar`, `cerrar`) le alcanzan al administrador para intervenir, o necesita algo más (por ejemplo, asignar a un motero en particular)? | Esos tres comandos. |
