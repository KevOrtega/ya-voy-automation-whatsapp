# Documentación de análisis y diseño — Ya Voy

Documentos de análisis y diseño del sistema, previos a la implementación. Están escritos en
español; todos los identificadores (tablas, columnas, estados, variables de entorno, claves de
plantillas) están en inglés, igual que el código.

| # | Documento | Contenido |
|---|-----------|-----------|
| 01 | [Requisitos](01-requisitos.md) | Actores, requisitos funcionales (RF), no funcionales (RNF), reglas de negocio (RN), datos del pedido, decisiones del cliente (DC) y preguntas abiertas (PA) |
| 02 | [Casos de uso](02-casos-de-uso.md) | Diagrama general y casos de uso con flujos principales y alternativos, incluidos el cambio de horario y el reporte diario |
| 03 | [Máquinas de estados](03-estados-pedido.md) | Estados del pedido y tabla de transiciones; ciclo de los cambios de horario y horario efectivo |
| 04 | [Secuencias](04-secuencias.md) | Recepción y validación; toma, carrera y liberación; alerta de pedido sin tomar; reporte diario con espera; cambio de horario con confirmación |
| 05 | [Modelo de datos](05-modelo-datos.md) | DER, tablas, restricciones, índices, alta automática de locales y moteros, cambios de horario, esquemas de extracción e interpretación |
| 06 | [Arquitectura](06-arquitectura.md) | C4 niveles 1 y 2, tareas programadas, entorno de desarrollo, mapeo a Dokku, riesgos, variables de entorno |
| 07 | [Catálogo de mensajes](07-catalogo-mensajes.md) | Todas las plantillas que envía el bot, cuándo y a quién |

## Glosario

| Término | Significado | Identificador en código |
|---------|-------------|-------------------------|
| Local | Quien escribe al bot por privado para pedir un envío (alta automática) | `store` |
| Motero / repartidor | Participante del grupo que toma pedidos (alta automática al reaccionar) | `courier` |
| Administrador | El cliente (jefe de los moteros); recibe alertas y el reporte | `admin` |
| Grupo de moteros | Grupo de WhatsApp donde se publican los pedidos | `couriers_group` |
| Borrador | Pedido en construcción, todavía incompleto | `order` con `status = 'draft'` |
| Publicación | Mensaje único del pedido en el grupo de moteros | `orders.group_message_id` |
| Tomar un pedido | Reaccionar con cualquier emoji a la publicación; el pedido queda resuelto (no hay seguimiento de entrega) | `claim` |
| Liberar un pedido | Quitar la reacción (solo el motero asignado) o comando del admin | `release` |
| Pedido sin tomar | Pedido publicado sin motero por más de `UNCLAIMED_ALERT_MINUTES`; se alerta al admin | `unclaimed_alert_sent_at` |
| Pedido pendiente | Pedido del día en `draft` o `published`; retiene el envío del reporte diario | `status IN ('draft','published')` |
| Horario base | Franja configurada en la que se aceptan pedidos nuevos | `BUSINESS_HOURS_START`, `BUSINESS_HOURS_END` |
| Cierre | Restricción del horario cargada por el admin en lenguaje natural y confirmada | `schedule_overrides` |
| Horario efectivo | Horario base menos cierres activos | `is_accepting_orders` |
| Reporte diario | Excel con los pedidos creados en el día, enviado al admin cuando no quedan pendientes | `daily_reports` |
| `pushName` | Nombre de perfil que el usuario se puso en WhatsApp | `display_name` |
| LID | Identificador anónimo de WhatsApp (`xxxx@lid`) usado en grupos en lugar del número | `lid` |
| JID | Identificador de chat de WhatsApp (`549...@s.whatsapp.net`, `...@g.us`) | `jid` |

## Convenciones

- Diagramas en [Mermaid](https://mermaid.js.org/), se ven directamente en GitHub/GitLab.
- Referencias cruzadas: `RF-xx` (funcional), `RNF-xx` (no funcional), `RN-xx` (regla de negocio),
  `CU-xx` (caso de uso), `Txx` (transición), `DC-xx` (decisión del cliente), `PA-xx` (pregunta abierta),
  `R-xx` (riesgo), claves de plantilla en `snake_case`.
- Los valores configurables se indican con su variable de entorno (por ejemplo `UNCLAIMED_ALERT_MINUTES`).
