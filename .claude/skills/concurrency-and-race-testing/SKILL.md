---
name: concurrency-and-race-testing
description: Use when implementing or testing anything that changes order status (claim, release, cancel, confirm, expire), webhook deduplication, the outbound queue, courier auto-registration or scheduled sweeps in Ya Voy - covers the Postgres patterns the docs mandate and how to prove them with deterministic race tests against a real database
---

# Concurrency and race testing

The auditor does not accept a stage that touches these paths without tests for atomic assignment
and the race cases. The guarantees come from Postgres, so the tests must run against **real
PostgreSQL 16** (the compose `postgres` service). SQLite or mocks cannot reproduce row locks,
re-evaluation after a lock wait, partial unique indexes or `SKIP LOCKED`; a race test on them
proves nothing.

## Patterns the docs mandate

| Case | Pattern | Source |
|------|---------|--------|
| Claim (first reaction wins) | `UPDATE orders SET courier_id=:c, status='assigned' ... WHERE id=:id AND courier_id IS NULL AND status='published' RETURNING id`. 1 row = winner, 0 rows = loser. Only after 0 rows, read the order to choose the message (T11 other courier, T12 same courier, T19 cancelled); never to decide the assignment. | RF-23, RN-02, D-02, docs/03 SQL, docs/04 §2 |
| Release by reaction removal | Conditional `UPDATE ... WHERE courier_id=:sender AND status='assigned'`; 0 rows = ignored (T14). | RF-28/29, RN-04, T13/T14 |
| Cancel vs claim | `SELECT status, courier_id ... FOR UPDATE`, then `UPDATE ... WHERE status=:status_read`. Courier gets `courier_order_cancelled` iff they really held the order. | RF-38, D-15, T16/T17, docs/04 §5 |
| Confirm/publish | `UPDATE ... WHERE status='awaiting_confirmation' AND store_id=:s RETURNING id`. | T06 |
| Webhook dedup | `INSERT INTO inbound_events ... ON CONFLICT (wa_message_id) DO NOTHING RETURNING id`; enqueue only when a row comes back. | RF-59, CU-11 |
| Job runs twice | Worker takes the event `FOR UPDATE` and checks `status='received'`; transitions are conditional; outbox keys are unique. | CU-11 alt. flows, RNF-03 |
| Outbound idempotency | Insert into `outbound_messages` in the **same transaction** as the state change, deterministic `idempotency_key` (e.g. `order:42:claimed:courier:7`), skip if it exists; enqueue `send_outbound` after `COMMIT`; sender takes the row `FOR UPDATE SKIP LOCKED`, FIFO per `recipient_jid`. | RF-60, D-03, docs/04 §9 |
| Store message bursts | Redis lock per store + `SELECT ... FOR UPDATE` of the draft; partial unique index `orders_one_draft_per_store`. | RNF-05, D-04, RN-09 |
| Sweeps (alerts, reports) | `SELECT ... FOR UPDATE SKIP LOCKED`, compare with `now()`, record the effect so a rerun is a no-op. | T15, CU-16/17, D-09, RF-54 |
| Invariant | `CHECK ((status = 'assigned') = (courier_id IS NOT NULL))` (`orders_courier_matches_status`). | docs/05 |

Keep the default `READ COMMITTED` isolation for these statements. The claim relies on Postgres
re-checking the `WHERE` clause after the competing transaction commits; under `REPEATABLE READ` the
loser gets a serialization error instead of 0 rows.

## How to write the tests

1. **One connection per actor.** Each concurrent actor uses its own `AsyncSession`/connection from
   the engine. Never share a session between tasks.
2. **Prefer deterministic interleavings over hoping for a race.** Force the order explicitly:
   - Actor A opens a transaction and takes the lock (`SELECT ... FOR UPDATE`, or runs its `UPDATE`
     without committing).
   - Start actor B as an `asyncio` task; assert it is blocked (it has not finished after a short
     wait, or `pg_stat_activity.wait_event_type = 'Lock'` for its backend).
   - Commit (or roll back) A, await B, and assert B's outcome.
   Cover **both orders** for each pair (claim-then-cancel and cancel-then-claim).
3. **Add a stress variant** on top: N actors released together (`asyncio.Event` or a barrier), then
   `asyncio.gather`. Assert exactly one winner. Run it repeatedly in a loop to shake out flakiness.
4. **Assert the whole outcome, not just the return value:** final row state, `order_events` rows
   (exactly one `claimed`; `claim_rejected` for losers), `outbound_messages` rows and their keys
   (no duplicates), and that the CHECK constraint was never violated.
5. **Replays:** call the same handler twice with the same event / same `wa_message_id` and assert
   no extra state change, event or outbound row.
6. Isolate tests: a fresh schema via Alembic at session start, and truncation (or a separate database)
   between tests. Transaction-rollback isolation does not work here because the actors must commit
   to see each other.

## Minimum race test list

- Two (and N) couriers react at once: exactly one `assigned`, the rest get `courier_order_already_taken` (T10/T11, DC-08).
- Same courier changes emoji: no change (T12). Non-assigned courier removes reaction: no change (T14).
- Claim vs cancel, both orders: cancel-first leaves 0 claim rows and `reaction_on_cancelled` (T19);
  claim-first ends `cancelled` with `courier_order_cancelled` queued after `courier_order_assigned` (T17, R-12).
- Release vs re-claim by another courier.
- Duplicate webhook delivery, sequential and concurrent: one `inbound_events` row, one enqueue (RF-59).
- Same inbound job processed twice: no duplicate effects (RNF-03).
- Two workers sending the outbox: each row sent once, FIFO per recipient kept (RF-60).
- A brand-new courier's two reactions arrive concurrently: one `couriers` row (unique `lid` / `phone_jid`), no crash.
- Concurrent burst from one store: one draft (RN-09).
- Alert/report sweep run by two workers: one alert per period, one report send per recipient (T15, RF-54).
