# Evolution API v2.3.7 webhook fixtures

Hand-built from the Evolution API **v2.3.7** source (`sendDataWebhook` envelope in
`webhook.controller.ts` and `prepareMessage` in `whatsapp.baileys.service.ts`), not captured from a
real number. All phones, JIDs, LIDs, names and texts are fake.

What is grounded in the source:

- Envelope: `event` (`messages.upsert`, `connection.update`), `instance`, `data`, `destination`,
  `date_time`, `sender`, `server_url`, `apikey`.
- `data` for messages: `key` (as Baileys delivers it, including `participant`, `remoteJidAlt`,
  `participantAlt`, `addressingMode`), `pushName`, `message`, `contextInfo`, `messageType`,
  `messageTimestamp`. `extendedTextMessage` is normalized to `conversation`. A private `@lid`
  `remoteJid` is replaced by `remoteJidAlt` when Baileys provides it.
- `connection.update` data: `instance`, `state` (`open`/`close`/`connecting`), `statusReason`, ...

**Pending real-number verification (`TODO(verify-evolution)`, see
`docs/plans/real-number-verification.md`):** which alternative fields really arrive
(`participantAlt`, `senderPn`, ...), whether a reaction removal arrives as an empty-text
`reactionMessage` and whether it reuses the original reaction's `key.id`
(`group_reaction_removal.json` assumes a new id), and how `@lid`-only chats look. Replace these
files with anonymized captures (`scripts/anonymize_captures.py`) once the verification runs.
