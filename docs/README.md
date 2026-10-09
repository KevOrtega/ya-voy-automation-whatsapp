# Documentación de análisis y diseño — Ya Voy

Documentos de análisis y diseño del sistema, previos a la implementación. Están escritos en
español; todos los identificadores (tablas, columnas, estados, variables de entorno, claves de
plantillas) están en inglés, igual que el código.

| # | Documento | Contenido |
|---|-----------|-----------|
| 01 | [Requisitos](01-requisitos.md) | Actores, requisitos funcionales (RF), no funcionales (RNF), reglas de negocio (RN), supuestos y preguntas abiertas para el cliente |
| 02 | [Casos de uso](02-casos-de-uso.md) | Diagrama general y casos de uso con flujos principales y alternativos |
| 03 | [Estados del pedido](03-estados-pedido.md) | Máquina de estados del pedido y tabla de transiciones |
| 04 | [Secuencias](04-secuencias.md) | Diagramas de secuencia: recepción y validación; toma, carrera y liberación |
| 05 | [Modelo de datos](05-modelo-datos.md) | DER, tablas, restricciones e índices |
| 06 | [Arquitectura](06-arquitectura.md) | C4 niveles 1 y 2, componentes, entorno de desarrollo, mapeo a Dokku, variables de entorno |
| 07 | [Catálogo de mensajes](07-catalogo-mensajes.md) | Todas las plantillas que envía el bot, cuándo y a quién |

## Glosario

| Término | Significado | Identificador en código |
|---------|-------------|-------------------------|
| Local | Comercio que genera pedidos de envío | `store` |
| Motero / repartidor | Persona que retira y entrega el pedido | `courier` |
| Grupo de moteros | Grupo de WhatsApp donde se publican los pedidos | `couriers_group` |
| Borrador | Pedido en construcción, todavía incompleto | `order` con `status = 'draft'` |
| Publicación | Mensaje del pedido en el grupo de moteros | `group_post` |
| Tomar un pedido | Reaccionar con cualquier emoji a la publicación | `claim` |
| Liberar un pedido | Quitar la reacción (solo el motero asignado) | `unclaim` |
| LID | Identificador anónimo de WhatsApp (`xxxx@lid`) usado en grupos en lugar del número | `lid` |
| JID | Identificador de chat de WhatsApp (`549...@s.whatsapp.net`, `...@g.us`) | `jid` |

## Convenciones

- Diagramas en [Mermaid](https://mermaid.js.org/), se ven directamente en GitHub/GitLab.
- Referencias cruzadas: `RF-xx` (funcional), `RNF-xx` (no funcional), `RN-xx` (regla de negocio),
  `CU-xx` (caso de uso), `PA-xx` (pregunta abierta), claves de plantilla en `snake_case`.
- Los valores configurables se indican con su variable de entorno (por ejemplo `CLAIM_TIMEOUT_MINUTES`).
