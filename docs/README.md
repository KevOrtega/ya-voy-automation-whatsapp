# Documentación de análisis y diseño — Ya Voy

Documentos de análisis y diseño del sistema, previos a la implementación. Están escritos en
español; todos los identificadores (tablas, columnas, estados, variables de entorno, claves de
plantillas) están en inglés, igual que el código.

| # | Documento | Contenido |
|---|-----------|-----------|
| 01 | [Requisitos](01-requisitos.md) | Actores, requisitos funcionales (RF), no funcionales (RNF), reglas de negocio (RN), datos del pedido, decisiones del cliente (DC) y preguntas abiertas (PA) |
| 02 | [Casos de uso](02-casos-de-uso.md) | Diagrama general y casos de uso con flujos principales y alternativos: confirmación del local, cancelación, cambios de horario, reporte diario y sus correcciones |
| 03 | [Máquinas de estados](03-estados-pedido.md) | Estados del pedido (con confirmación del local) y tabla de transiciones; ciclo de los cambios de horario, horario efectivo y día operativo |
| 04 | [Secuencias](04-secuencias.md) | Recepción, validación y confirmación; toma, carrera y liberación; alerta de pedido sin tomar; cancelación (carrera con la toma y eliminación en el grupo); reporte diario con espera y corrección; cambio de horario; bandeja de salida |
| 05 | [Modelo de datos](05-modelo-datos.md) | DER, tablas, restricciones, índices, alta automática de locales y moteros, cambios de horario, reporte con versiones, esquemas de extracción e interpretación |
| 06 | [Arquitectura](06-arquitectura.md) | C4 niveles 1 y 2, tareas programadas, entorno de desarrollo, mapeo a Dokku, riesgos, variables de entorno |
| 07 | [Catálogo de mensajes](07-catalogo-mensajes.md) | Todas las plantillas que envía el bot, cuándo y a quién |

## Glosario

| Término | Significado | Identificador en código |
|---------|-------------|-------------------------|
| Local | Quien escribe al bot por privado para pedir un envío (alta automática) | `store` |
| Motero / repartidor | Participante del grupo que toma pedidos (alta automática al reaccionar) | `courier` |
| Administrador | El cliente (jefe de los moteros); recibe alertas y el reporte, cancela o libera pedidos y cambia el horario | `admin` |
| Grupo de moteros | Grupo de WhatsApp donde se publican los pedidos | `couriers_group` |
| Borrador | Pedido en construcción, todavía incompleto | `order` con `status = 'draft'` |
| Pedido a confirmar | Pedido completo cuyo resumen se envió al local y espera su *sí* | `status = 'awaiting_confirmation'` |
| Publicación | Mensaje único del pedido en el grupo de moteros | `orders.group_message_id` |
| Tomar un pedido | Reaccionar con cualquier emoji a la publicación; el pedido queda resuelto (no hay seguimiento de entrega) | `claim` |
| Liberar un pedido | Quitar la reacción (solo el motero asignado) o comando del admin | `release` |
| Pedido sin tomar | Pedido publicado sin motero por más de `UNCLAIMED_ALERT_MINUTES`; se alerta al admin | `unclaimed_alert_sent_at` |
| Cancelar un pedido | Lo pide el local o el admin; se elimina la publicación del grupo y se avisa al motero si lo tenía | `cancel`, `group_order_delete` |
| Pedido pendiente | Pedido del día en `draft`, `awaiting_confirmation` o `published`; retiene el reporte o su corrección | `status IN ('draft','awaiting_confirmation','published')` |
| Día operativo | Día al que pertenece un pedido; empieza a `BUSINESS_DAY_ROLLOVER_TIME` (05:00), así lo de después de medianoche cuenta para el día anterior | `business_date` |
| Horario base | Franja configurada en la que se aceptan pedidos nuevos | `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END` |
| Cierre | Restricción del horario cargada por el admin en lenguaje natural y confirmada | `schedule_overrides` (`closed_*`) |
| Apertura extra | Ampliación del horario cargada por el admin ("hoy hasta las 00") y confirmada | `schedule_overrides` (`open_window`) |
| Horario efectivo | Horario base más aperturas extra menos cierres activos; su fin es el cierre efectivo del día | `is_accepting_orders`, `report_due_at` |
| Reporte diario | Excel con los pedidos del día operativo, enviado al admin al cierre efectivo cuando no quedan pendientes | `daily_reports`, `report_revisions` |
| Corrección del reporte | Nueva versión del reporte cuando, después de enviado, un pedido cambió de motero o se canceló, o se agregaron pedidos por una apertura extra | `report_revisions.kind = 'correction'` |
| `pushName` | Nombre de perfil que el usuario se puso en WhatsApp | `display_name` |
| LID | Identificador anónimo de WhatsApp (`xxxx@lid`) usado en grupos en lugar del número | `lid` |
| JID | Identificador de chat de WhatsApp (`549...@s.whatsapp.net`, `...@g.us`) | `jid` |

## Convenciones

- Diagramas en [Mermaid](https://mermaid.js.org/), se ven directamente en GitHub/GitLab.
- Referencias cruzadas: `RF-xx` (funcional), `RNF-xx` (no funcional), `RN-xx` (regla de negocio),
  `CU-xx` (caso de uso), `Txx` (transición), `DC-xx` (decisión del cliente), `PA-xx` (pregunta abierta),
  `R-xx` (riesgo), claves de plantilla en `snake_case`.
- Los valores configurables se indican con su variable de entorno (por ejemplo `UNCLAIMED_ALERT_MINUTES`).
