# Real-number verification (Evolution API v2.3.7)

Checklist for the verification task scheduled before stage 4. It answers the open questions in
`.claude/skills/evolution-api-events/SKILL.md` ("Pending verification", numbered Q1-Q5 below) with
real payloads, and replaces the hand-built fixtures in `tests/fixtures/evolution/`.

## Ground rules

- The bot uses a **test number**, never the client's real number. Participants use their own phones;
  their numbers end up only in `data/` (gitignored) and are anonymized before anything is committed
  (RNF-09).
- Test group: the bot + **at least two participants** (A and B). One more phone acts as a store (S);
  one participant is set as admin in `ADMIN_PHONES` for scenario 9.
- Development stack only (`docker compose up`), `APP_ENV=development`.

## Setup

1. `.env`: `CAPTURE_INBOUND_PAYLOADS=1`, `COURIERS_GROUP_JID=<test group jid>`,
   `ADMIN_PHONES=<admin test phone>`. Restart `web`.
2. Link the test number in the Evolution manager (`http://localhost:8080/manager`) and set the
   instance webhook to `http://web:8000/webhooks/evolution/<WEBHOOK_SECRET>`, `webhookByEvents=false`.
   Events: `MESSAGES_UPSERT`, `CONNECTION_UPDATE`. **During the verification only**, also enable
   `MESSAGES_UPDATE`, `MESSAGES_DELETE` and `SEND_MESSAGE`: the webhook answers `ignored` for them,
   but the capture keeps their shape (edits and deletes may arrive there, not as upserts).
3. Every payload lands in `data/captures/<utc time>_<event>_<hash>.json` (secret fields already
   redacted). Write down the file names per scenario as you go.
4. Sends (scenarios 6-8, 10) go through Evolution directly (manager or `curl` with the API key) or by
   inserting an `outbound_messages` row and enqueuing `send_outbound`; note the returned `key.id`.

## Scenarios

| # | Scenario | Do | Record | Questions |
|---|----------|----|--------|-----------|
| 1 | Private text | S writes a short text to the bot; then a text quoting a bot message. | `key.remoteJid` (number or `@lid`?), `remoteJidAlt`, `addressingMode`, `pushName`, `messageType` (`conversation` after normalization), `contextInfo.stanzaId`. | Q4 (shape of private chats), quote (RF-34) |
| 2 | Group text | A writes a text in the group. | `key.participant`, `participantAlt`, `senderPn`; confirm the webhook answers `ignored` (`group_not_reaction`). | Q2 |
| 3 | Reaction add | The bot posts a text in the group; A reacts with 👍. | `reactionMessage.key.id` = the post's `key.id`? the reaction's own `key.id`; participant fields. | Q2, dedup key |
| 4 | Near-simultaneous reactions | A and B react to the same post within ~1 s. | Both events arrive, distinct `key.id`s, order and timestamps (`messageTimestamp`, `senderTimestampMs`). | Q2, race (RN-04) |
| 5a | Reaction removal by the reacting participant | A removes the reaction from scenario 3. | Arrives as `messages.upsert`? `reactionMessage.text` empty or missing? **Same or new `key.id` as the reaction?** | **Q1** |
| 5b | Reaction removal by a non-assigned participant | B reacts, then removes it. | Same as 5a for a second person; compare `key.id` reuse. | Q1 |
| 5c | Change of emoji | A reacts 👍 then changes it to ❤️. | One event or removal + add? `key.id` reuse? | Q1 |
| 6 | Delete for everyone | `DELETE /chat/deleteMessageForEveryone/{instance}` on the bot's post (`fromMe: true`). Repeat on an old post (> 2 days if possible). | HTTP status and body; what the group sees; events received (`MESSAGES_UPDATE`/`MESSAGES_DELETE`?). | Q5 (R-11) |
| 7 | Edit | `POST /chat/updateMessage/{instance}` on a bot post with new text. Repeat on an old post. | HTTP status and body; group view; events received. | Q5 (edit fallback) |
| 8 | Quoted reply in the group | `sendText` to the group with `quoted: {key: {id: <post id>}}`. | The reply quotes the post (needs `DATABASE_SAVE_DATA_NEW_MESSAGE=true`). | RF-37 fallback |
| 9 | Admin writing from a `@lid` chat | The admin phone writes to the bot (ideally a phone whose chat shows as `@lid`). | `remoteJid` number or `@lid`? Which field carries the number? | **Q4** (S-07, R-06) |
| 10 | Private send to a `@lid`-only participant | `sendText` with `number` = the `@lid` of A/B taken from scenario 3 (when no number arrived). | Delivered? HTTP status; what A sees. | **Q3** (S-03, R-03) |
| 11 | Connection update | Log the bot out from the phone, then link it again. | `connection.update` payloads (`state`, `statusReason`). | `admin_whatsapp_disconnected` |

## Questions to answer (from the skill)

- **Q1 Reaction removal:** shape of the removal and whether it reuses the reaction's `key.id`. If it
  does, change only `compute_inbound_dedup_key` in `ya_voy/whatsapp/dedup.py` (hash of `key.id` +
  reaction text + `messageTimestamp`, docs/05 `inbound_events`) and its tests.
- **Q2 `@lid` in groups:** which fields carry the phone and when; drives the courier merge (RF-22).
- **Q3 Private messages to `@lid`-only couriers:** delivered or not.
- **Q4 Admin identity:** number or `@lid` in the private chat; whether `ADMIN_JIDS` is needed.
- **Q5 Delete and edit in the group:** results for recent and old messages.

## After the session

1. `python -m scripts.anonymize_captures data/captures tests/fixtures/evolution/captured --mapping data/captures/pseudonyms.json`
   and review every `review ...` line it prints. Never commit the mapping file.
2. Replace or complement the hand-built fixtures; update `tests/fixtures/evolution/README.md` and
   remove the `TODO(verify-evolution)` notes that are answered.
3. Write the answers (Q1-Q5) into the skill and docs/05/06 (S-03, S-07, R-03, R-06, R-11).
4. Replay fixtures against a running stack: `python -m scripts.replay_fixture <files> --times 2`.
5. Set `CAPTURE_INBOUND_PAYLOADS=0` and remove the extra webhook events.
