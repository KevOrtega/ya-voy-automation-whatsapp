# Pendientes antes de producción

Tareas que no pertenecen a una etapa ya entregada pero que **deben** estar hechas antes de poner el bot
en producción con el número real del cliente. Cada una cita los requisitos que la exigen.

## 1. Retención de datos (`apply_data_retention`) — hecha

**Requisitos:** RNF-10 y DC-11 (docs/01): a los `DATA_RETENTION_DAYS` (90) días se anonimizan direcciones,
notas, textos de mensajes, payloads crudos y archivos de reportes; se conservan códigos, estados, tiempos y
conteos. Relacionados: RNF-09 (privacidad), docs/05 (`orders.anonymized_at`, `messages.body`,
`report_revisions.file_content`, `parked_reactions`), docs/06 (tabla de tareas programadas y sección
"Retención de datos").

**Estado:** hecha (rama `data-retention`). `ya_voy/retention/service.py` y la tarea cron
`apply_data_retention` (cada hora en el minuto 17 y al arrancar), migración `0004` (columnas
`anonymized_at` e índices parciales), `DATA_RETENTION_BATCH_SIZE`, tests en
`tests/integration/test_retention.py`. Diseño y decisiones en docs/06, "Retención de datos" (D-20).
Diferencias con el alcance original de abajo, por indicación de la auditoría: no se borra ninguna fila
(tampoco `report_revisions` ni `parked_reactions`, que además no tienen datos personales); se anonimizan
también los mensajes, eventos y cambios de horario viejos aunque el día no esté cerrado, salvo los ligados a
flujos vivos; y las capturas de desarrollo viejas de `CAPTURE_DIR` se borran del disco.

**Alcance mínimo:**

- Tarea cron diaria `apply_data_retention`, idempotente y recuperable (compara contra la hora actual, como
  las demás; docs/06 D-09), en transacciones cortas y por lotes.
- `orders` con `business_date` más vieja que `DATA_RETENTION_DAYS` y en estado final (`cancelled`,
  `expired`, `assigned` de días cerrados): vaciar `pickup_address`, `delivery_address`, `product`,
  `payment_method_raw`, `notes` y `extraction`; poner `anonymized_at`. Se conservan `code`, estados,
  tiempos, `courier_id`/`last_courier_id` y `store_id` (los conteos del reporte no cambian).
- `messages.body`, `inbound_events.payload` (reemplazar por un objeto vacío o mínimo sin texto),
  `outbound_messages.body` y `last_error`, `order_events.data` con texto libre,
  `schedule_overrides.source_text`/`parsed`, `report_revisions.file_content` (borrar) y
  `parked_reactions` resueltas (borrar).
- Nunca tocar un reporte que todavía puede corregirse (estado distinto de `sent`/`failed`) ni pedidos
  pendientes.
- Las capturas de `data/captures` (solo desarrollo) quedan fuera: no existen en producción.

**Criterios de aceptación:** tests de integración con reloj congelado que verifiquen exactamente qué
columnas se vacían y cuáles se conservan; que correr la tarea dos veces no cambia nada; que el reporte de un
día ya anonimizado sigue dando los mismos conteos; y que no se bloquea a la toma de pedidos (lotes con
`FOR UPDATE SKIP LOCKED`).
