# 01 — Especificación de requisitos

## 1. Propósito y alcance

Ya Voy automatiza la operación de un despachante de envíos que hoy coordina todo por WhatsApp:
los locales mandan pedidos, alguien los revisa y los reenvía a un grupo de moteros, y el motero
que lo toma avisa. El sistema reemplaza esa intermediación manual por un bot que:

1. Recibe pedidos de locales registrados en un número de WhatsApp dedicado.
2. Extrae los datos del pedido y valida que estén completos.
3. Pide al local los datos faltantes, admitiendo que el pedido llegue en varios mensajes.
4. Publica el pedido completo en el grupo de moteros.
5. Asigna el pedido al primer motero que reacciona a la publicación, con garantía de un único ganador.
6. Notifica a cada parte (local, motero, grupo) en cada cambio de estado.

**Fuera de alcance de la primera versión (v1):** cobro o liquidación a moteros, cálculo de tarifas,
seguimiento GPS, panel web de administración (ver PA-09), integración con la API oficial de WhatsApp
(Cloud API) y procesamiento de audios o imágenes (ver PA-06).

## 2. Actores

| Actor | Identificador | Descripción |
|-------|---------------|-------------|
| Local | `store` | Comercio registrado que envía pedidos al número del bot por chat privado. Se identifica por su número de teléfono. |
| Motero | `courier` | Repartidor registrado, miembro del grupo de moteros. Toma pedidos reaccionando en el grupo y recibe confirmaciones por privado. Se identifica por teléfono y/o LID. |
| Sistema | `system` | El bot: API, worker y tareas programadas (vencimientos, republicaciones). |
| Administrador | `admin` | Persona del cliente que da de alta locales y moteros, recibe alertas y puede intervenir pedidos. |
| Servicios externos | — | Evolution API (pasarela de WhatsApp) y el proveedor de LLM. No son actores de negocio, pero aparecen en las secuencias. |

## 3. Requisitos funcionales

### Recepción y validación de pedidos

| ID | Requisito |
|----|-----------|
| RF-01 | El sistema recibe los eventos de WhatsApp mediante un webhook de Evolution API. |
| RF-02 | El sistema acepta pedidos solo de números de locales registrados y activos. Los mensajes de otros números se responden con un mensaje genérico (ver RN-10) y no generan pedidos. |
| RF-03 | Por cada mensaje de texto de un local, el sistema extrae los datos del pedido mediante un LLM a un esquema estructurado (`OrderExtraction`). |
| RF-04 | La validación de campos obligatorios y de formato se hace con código determinístico, no con el LLM. |
| RF-05 | Si el local no tiene un borrador abierto, el mensaje crea un borrador nuevo. Si tiene uno, los datos extraídos se combinan con el borrador existente (los valores nuevos no vacíos reemplazan a los anteriores). |
| RF-06 | El sistema espera un breve intervalo sin mensajes nuevos del local (`DRAFT_DEBOUNCE_SECONDS`) antes de evaluar el borrador y responder, para no contestar a cada mensaje de una ráfaga. |
| RF-07 | Si el borrador está incompleto, el sistema responde al local indicando exactamente qué campos faltan o son inválidos. |
| RF-08 | Si el borrador está completo, el sistema lo publica en el grupo de moteros, confirma al local con un código de pedido y un resumen. |
| RF-09 | Un borrador sin actividad durante `DRAFT_TTL_MINUTES` (por defecto 30) vence; el sistema avisa al local. |
| RF-10 | El local puede descartar el borrador en curso con el comando `cancelar`. |

### Asignación (toma) y liberación

| ID | Requisito |
|----|-----------|
| RF-11 | Un motero registrado toma un pedido reaccionando con cualquier emoji a una publicación del pedido en el grupo. |
| RF-12 | La asignación es atómica en la base de datos: si varios moteros reaccionan casi al mismo tiempo, gana exactamente uno. |
| RF-13 | Al ganador se le envía un mensaje privado con la confirmación y el detalle completo del pedido. |
| RF-14 | Se notifica al local quién tomó el pedido. |
| RF-15 | Se responde en el grupo, citando la publicación, "Asignado a {motero}". |
| RF-16 | La reacción de un motero que no ganó se ignora para la asignación; opcionalmente (`NOTIFY_LOSER_ON_RACE`) se le avisa por privado que el pedido ya fue tomado. |
| RF-17 | Si el motero asignado quita su reacción, el pedido se libera y vuelve a estar disponible; se notifica al local y al grupo. |
| RF-18 | Quitar una reacción solo libera el pedido si quien la quita es el motero asignado. |
| RF-19 | Cambiar de emoji no libera ni reasigna el pedido. |
| RF-20 | Las reacciones de números no registrados como moteros se ignoran y se registran; se alerta al administrador. |

### Vencimientos, cancelación y entrega

| ID | Requisito |
|----|-----------|
| RF-21 | Si un pedido publicado no es tomado en `CLAIM_TIMEOUT_MINUTES`, el sistema lo vuelve a publicar en el grupo y avisa al local. |
| RF-22 | Tras `MAX_REPOSTS` republicaciones sin toma, el pedido pasa a `unclaimed`; se avisa al local y al administrador. El pedido sigue pudiendo tomarse reaccionando a cualquiera de sus publicaciones. |
| RF-23 | El local puede cancelar un pedido publicado o asignado con `cancelar {código}` o respondiendo (citando) el mensaje de confirmación con `cancelar`. |
| RF-24 | Al cancelarse un pedido asignado, se avisa al motero por privado y al grupo. |
| RF-25 | El motero asignado marca el pedido como entregado enviando por privado `entregado {código}`, o `entregado` citando el mensaje de asignación, o solo `entregado` si tiene un único pedido asignado. |
| RF-26 | Al marcarse como entregado, se notifica al local y se confirma al motero. |

### Administración, auditoría e idempotencia

| ID | Requisito |
|----|-----------|
| RF-27 | El administrador puede dar de alta, modificar y desactivar locales y moteros (v1: API interna protegida con `ADMIN_API_KEY` y/o script de carga). |
| RF-28 | El administrador puede consultar pedidos y su historial de eventos, y forzar cancelación, liberación o asignación. |
| RF-29 | Todo cambio de estado de un pedido queda registrado en un log de auditoría (`order_events`). |
| RF-30 | Todos los mensajes entrantes y salientes quedan registrados como historial de conversación (`messages`). |
| RF-31 | Los eventos duplicados del webhook (mismo id de mensaje de WhatsApp) se procesan una sola vez. |
| RF-32 | Los mensajes salientes se envían a través de una bandeja de salida (`outbound_messages`) con clave de idempotencia, para no duplicar notificaciones ante reintentos. |

## 4. Requisitos no funcionales

| ID | Requisito |
|----|-----------|
| RNF-01 | **Latencia del webhook.** El endpoint del webhook responde en menos de 200 ms (p95): persiste el evento crudo, encola y devuelve `200`. Todo el procesamiento ocurre en el worker. |
| RNF-02 | **Latencia de negocio.** Desde que un motero reacciona hasta que recibe la confirmación por privado: < 5 s (p95), excluyendo demoras de WhatsApp. |
| RNF-03 | **Idempotencia.** Reprocesar un mismo evento no produce efectos duplicados (ni cambios de estado ni mensajes). Se garantiza con restricciones únicas en Postgres. |
| RNF-04 | **Consistencia.** Postgres es la fuente de verdad. Redis solo guarda la cola de trabajos y locks efímeros; perder Redis no debe perder pedidos ni borradores. |
| RNF-05 | **Concurrencia.** Los mensajes de un mismo local se procesan en serie (lock por local) para que la combinación de borradores sea consistente. |
| RNF-06 | **Riesgo de bloqueo del número.** Evolution API usa Baileys (cliente no oficial de WhatsApp Web). Se usa un número dedicado, nunca el personal del cliente; no se envían mensajes masivos ni a contactos que no escribieron primero (salvo moteros registrados); los envíos salientes se limitan (`OUTBOUND_MIN_INTERVAL_MS`). |
| RNF-07 | **Disponibilidad.** Si Evolution o el LLM no responden, los trabajos se reintentan con backoff; el pedido no se pierde. Si el LLM falla de forma persistente, se avisa al local que reintente y al administrador. |
| RNF-08 | **Observabilidad.** Logs estructurados (JSON) con `order_id`, `store_id`, `courier_id` y `wa_message_id`; endpoint `/health`. |
| RNF-09 | **Seguridad.** El webhook exige un secreto compartido; la API de administración exige `ADMIN_API_KEY`; secretos solo por variables de entorno. Los datos personales de clientes finales (nombre, teléfono, dirección) se retienen `DATA_RETENTION_DAYS` días (ver PA-11). |
| RNF-10 | **Portabilidad.** Todos los servicios corren en contenedores Docker; desarrollo con Docker Compose y producción en Dokku sin cambios de código (solo configuración). |
| RNF-11 | **Proveedor de LLM intercambiable.** La extracción se hace detrás de una interfaz propia; cambiar de proveedor o modelo es configuración. |
| RNF-12 | **Idioma.** Código, identificadores y commits en inglés; mensajes al usuario en español, centralizados en el catálogo de mensajes. |
| RNF-13 | **Zona horaria.** Las fechas se guardan en UTC (`timestamptz`) y se muestran en `America/Argentina/Buenos_Aires`. |

## 5. Reglas de negocio

| ID | Regla |
|----|-------|
| RN-01 | Un pedido tiene a lo sumo un motero asignado en cada momento. |
| RN-02 | Gana la asignación el primer motero cuya reacción se procesa con éxito en la base (`UPDATE ... WHERE courier_id IS NULL`), no el primero en el orden de llegada del webhook. |
| RN-03 | Cualquier emoji cuenta como toma. |
| RN-04 | Solo el motero asignado puede liberar el pedido quitando su reacción. Quitar la reacción de un motero no asignado no tiene efecto. |
| RN-05 | Un cambio de emoji del motero asignado no cambia nada. Un cambio de emoji de un motero no asignado se trata como un intento de toma (normalmente ignorado porque ya está asignado). |
| RN-06 | Un pedido solo puede liberarse por reacción mientras está `assigned`. Después de `delivered` o `cancelled`, quitar la reacción no tiene efecto. |
| RN-07 | Solo el motero asignado puede marcar el pedido como entregado. |
| RN-08 | Solo el local dueño del pedido (o el administrador) puede cancelarlo. Un pedido `delivered` no se puede cancelar. |
| RN-09 | Un local tiene como máximo un borrador abierto a la vez. |
| RN-10 | Los mensajes de números no registrados reciben una única respuesta genérica cada `UNKNOWN_SENDER_REPLY_COOLDOWN_HOURS` horas, para no conversar con desconocidos (riesgo de bloqueo). |
| RN-11 | Solo se consideran reacciones sobre mensajes que el bot publicó en el grupo de moteros configurado (`COURIERS_GROUP_JID`). Los demás mensajes del grupo se ignoran. |
| RN-12 | Un motero puede tener varios pedidos asignados a la vez, salvo que se configure `MAX_ACTIVE_ORDERS_PER_COURIER` (ver PA-05). |
| RN-13 | Los campos obligatorios del pedido son los del conjunto confirmado por el cliente (por defecto, sección 6). |
| RN-14 | Mientras un pedido está `published`, `assigned` o `unclaimed`, los mensajes nuevos del local abren un borrador nuevo (no modifican el pedido publicado). Las correcciones a un pedido publicado se hacen cancelándolo y enviándolo de nuevo (ver PA-04). |

## 6. Datos del pedido (propuesta por defecto)

Esquema que debe producir la extracción (`OrderExtraction`). Hay que confirmarlo con el cliente (PA-01).

| Campo | Identificador | Obligatorio | Validación determinística |
|-------|---------------|-------------|---------------------------|
| Local de retiro | `store_id` | Sí (implícito) | Se toma del remitente; dirección de retiro = dirección registrada del local, salvo que el mensaje indique otra (`pickup_address`). |
| Nombre del cliente | `customer_name` | Sí | No vacío, 2 a 80 caracteres. |
| Teléfono del cliente | `customer_phone` | Sí | Normalizable a E.164 de Argentina (`+549...`) con `phonenumbers`. |
| Dirección de entrega | `delivery_address` | Sí | No vacía; debe contener calle y altura, o una referencia explícita (ver PA-02). |
| Detalle / ítems | `items_description` | Sí | No vacío. |
| Monto a cobrar | `amount_to_collect` | Sí | Número ≥ 0 (0 si ya está pagado). |
| Medio de pago | `payment_method` | Sí | Uno de `cash`, `transfer`, `prepaid`. Si `amount_to_collect > 0`, no puede ser `prepaid`. |
| Observaciones | `notes` | No | Texto libre (piso, timbre, horario, etc.). |

## 7. Supuestos

- S-01. Habrá un único número de WhatsApp para el bot (una instancia de Evolution) y un único grupo de moteros.
- S-02. El número del bot es administrador del grupo de moteros o, al menos, miembro con permiso para escribir.
- S-03. Los moteros pueden recibir mensajes privados del número del bot (lo tienen agendado o ya hubo intercambio).
- S-04. Los locales escriben en texto. Las imágenes con texto (caption) se procesan solo por su texto.
- S-05. Los pedidos son para entrega inmediata; no hay pedidos programados en v1.
- S-06. El volumen inicial es bajo a medio (decenas a pocos cientos de pedidos por día), compatible con un solo VPS.

## 8. Preguntas abiertas para el cliente

| ID | Pregunta | Propuesta por defecto |
|----|----------|------------------------|
| PA-01 | ¿Los campos obligatorios de la sección 6 son correctos? ¿Falta alguno (por ejemplo, horario de retiro, zona, tamaño del paquete)? | El conjunto de la sección 6. |
| PA-02 | ¿Qué tan precisa debe ser la dirección? ¿Se valida contra un geocodificador o alcanza con texto libre? | Texto libre con calle y altura; sin geocodificación en v1. |
| PA-03 | ¿Se informa al local el nombre y teléfono del motero asignado? ¿Y al motero el teléfono del cliente final? | Al local: nombre y teléfono del motero. Al motero: todos los datos del pedido. |
| PA-04 | ¿El local puede modificar un pedido ya publicado, o alcanza con cancelar y reenviar? | Cancelar y reenviar en v1. |
| PA-05 | ¿Hay un máximo de pedidos simultáneos por motero? | Sin límite en v1. |
| PA-06 | ¿Los locales mandan audios o fotos de comandas? ¿Hace falta transcribir/leer? | v1 solo texto; a audios e imágenes sin texto se responde pidiendo texto. |
| PA-07 | ¿Cuántos minutos sin toma antes de republicar, y cuántas republicaciones antes de escalar? | `CLAIM_TIMEOUT_MINUTES=10`, `MAX_REPOSTS=2`. |
| PA-08 | ¿Se avisa al motero que perdió la carrera ("ya lo tomó otro")? | Sí (`NOTIFY_LOSER_ON_RACE=true`). |
| PA-09 | ¿Quién administra locales y moteros y cómo (panel web, planilla, mensajes al bot)? | v1: script de carga y API interna; panel en una versión posterior. |
| PA-10 | ¿El motero debe confirmar el retiro (`picked_up`) además de la entrega? | No en v1; solo `delivered`. |
| PA-11 | ¿Cuánto tiempo hay que conservar los datos de clientes finales? | 90 días (`DATA_RETENTION_DAYS=90`), luego anonimizar. |
| PA-12 | ¿Qué pasa con un pedido asignado que nunca se marca como entregado? | Recordatorio al motero a las `DELIVERY_REMINDER_MINUTES` (90) y alerta al admin; cierre manual. |
| PA-13 | ¿Hay horario de operación? ¿Qué se responde fuera de horario? | Sin restricción horaria en v1. |
| PA-14 | ¿Quién recibe las alertas de administración (números)? | Uno o más números en `ADMIN_PHONES`. |
