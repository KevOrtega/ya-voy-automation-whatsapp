---
name: evolution-api-events
description: Use when parsing Evolution API webhook payloads, resolving @lid / phone identities, handling reactions or reaction removal, sending/deleting/editing WhatsApp messages, or writing Evolution fixtures in Ya Voy - separates what the docs assume from what must still be verified with a real number
---

# Evolution API events

Evolution API v2 (Baileys, unofficial WhatsApp Web client) is the most changeable dependency. All
payload knowledge stays in `ya_voy/whatsapp` (parsing, jid/lid helpers) and `ya_voy/identity`
(sender classification, auto-registration, merge). Business code receives normalized events
(`ReactionAdded`, `ReactionRemoved`, `StoreTextReceived`, ...; docs/06 §2).

## What the docs assume

- Webhook: Evolution `POST`s to `/webhooks/evolution/{WEBHOOK_SECRET}` over the compose network;
  configured per instance with only `MESSAGES_UPSERT` and `CONNECTION_UPDATE` (docs/06 §3, §6).
- Ignore `fromMe`, groups other than `COURIERS_GROUP_JID`, `status@broadcast`, and anything in the
  group that is not a reaction to an order post (RF-02, RN-12).
- Dedup key: `data.key.id` -> `inbound_events.wa_message_id`; hash of the payload for events without an id (docs/05).
- Reaction: `messages.upsert` with `message.reactionMessage`; the reacted message is
  `reactionMessage.key.id` (matched against `orders.group_message_id`); a reaction has its own `key.id`.
  **Empty `reactionMessage.text` = removal.** Any emoji counts as a claim (RN-03).
- Group author: `key.participant` (`xxxx@lid` or `549...@s.whatsapp.net`), plus whatever alternative
  fields the pinned version delivers (`participantAlt`, `senderPn`, `remoteJidAlt`, ...). Identity is
  the participant id; the phone is optional (D-07, RN-11). Merge two courier rows when one event
  carries both ids (docs/05, R-04).
- Quoted message: `contextInfo.stanzaId` (store `cancelar` by quote, RF-34).
- Sending: `sendText`, `sendMedia` (report `.xlsx`), `deleteMessageForEveryone`
  (`DELETE /chat/deleteMessageForEveryone/{instance}`, bot's own messages only), `updateMessage` (edit
  fallback). Delete fails -> edit to CANCELADO -> quoted reply (RF-37, R-11). All via the outbox.
- `pushName` is the display name; fall back to `STORE_FALLBACK_NAME` / `COURIER_FALLBACK_NAME`.
- Evolution env var names in docs/06 §6 are "to be confirmed when the version is pinned".

## Pending verification (do not hardcode guesses)

These must be confirmed with a real test number and a test group (never the client's production
number) in the dedicated verification task, which is scheduled **before stage 4**:

1. **Reaction removal:** does it arrive as `messages.upsert` with empty text, and does it reuse the
   original reaction's `key.id`? If it reuses it, dedup by `wa_message_id` would drop the removal and
   the unique key becomes a hash of `key.id` + reaction text + `messageTimestamp` (note in docs/05,
   `inbound_events`).
2. **`@lid` in groups:** which fields carry the phone (`participantAlt`, `senderPn`, ...) and when
   (RF-22, docs/05 courier merge).
3. **Private messages to a courier known only by `@lid`:** do they deliver? (S-03, R-03; on failure,
   `admin_outbound_failed` and the group still shows "Asignado a").
4. **Admin identity:** is the admin's private chat exposed as a number or only as `@lid`? If only
   `@lid`, compare against alternative fields or add `ADMIN_JIDS` (S-07, R-06).
5. **Message deletion and edit in the group:** behaviour of `deleteMessageForEveryone` and
   `updateMessage`, including an old message WhatsApp no longer lets you delete (docs/06 §3, R-11).

Until verified, keep these behind small, well-named functions in `ya_voy/whatsapp` / `ya_voy/identity`
with a `TODO(verify-evolution)` note and tests built from the documented shape, so the fix is local
once real payloads exist. Do not write code that silently depends on an unverified assumption
elsewhere.

## Fixtures

docs/06 §3 plans fixtures from **real** Evolution payloads (text, reaction, reaction removal, quote,
`@lid` participant with and without alternative number, sent document) and a command to replay them
against the webhook. When capturing them, replace real phone numbers, names and message text with
fake ones before committing (RNF-09). Keep the original structure and field names untouched.
