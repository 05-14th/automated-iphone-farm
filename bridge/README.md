# bridge — sending worker + gateway API

Phase 12. The code that sits between other systems and BlueBubbles.

```
caller ──HTTP──▶ bridge/api ──▶ Supabase ◀── bridge/worker ──▶ BlueBubbles ──▶ Messages.app ──▶ recipient
                                   ▲                                              │
                                   └────── bridge/api/v1/webhooks/bluebubbles ◀────┘
```

The API never sends. It enqueues, and the worker sends. That separation is what
makes the per-conversation FIFO rule and the rate limit enforceable at all.

Read `docs/PHASE1-REPORT.md` and `docs/BRIDGE-SCHEMA.md` first. Everything below
is a consequence of what those two documents measured.

---

## The two things that drive the whole design

### 1. The HTTP response tells you nothing about delivery

Measured, in both directions:

- `POST /chat/new` returns **HTTP 500 after 75–120 s** — and the message is
  delivered, `error=0`. It also throws AppleScript `-1728 Can't get chat id`
  *after* the chat exists.
- `POST /message/text` into an existing chat returns a clean **200 in ~0.7 s**.
- A send issued while the Mac is **offline** returns **200** and delivers minutes
  later.

So a 5xx does not mean failed, and a 2xx does not mean delivered. A worker that
trusts the response and retries **will double-send**.

**The rule: never retry on a timeout or 5xx without first reconciling against
provider state.** The database enforces it — `trg_messages_state_machine` refuses
`failed → queued` and `failed → sent` unless `reconciled = true`. The worker
cannot skip the reconcile step even if it wants to.

### 2. Two send paths, not one

| Path | When | Latency | Response |
|---|---|---|---|
| `POST /chat/new` | **once**, the first message of a conversation | 75–120 s | always times out / 500 |
| `POST /message/text` | **every message after that** | ~0.7 s | clean 200 |

Each conversation is created **once** via `chat/new`. The timeout is expected and
tolerated; the reconcile pass then recovers both the message GUID and the chat
GUID that `chat/new` failed to return, and stores the chat GUID on
`conversations.provider_chat_guid`. From then on the conversation uses the fast
path forever.

Using `chat/new` as the normal send path would cap throughput at roughly one
message every two minutes and generate a timeout on *every single send*.

### And one consequence: duplicate webhooks are normal

BlueBubbles dispatches the same `new-message` event twice, ~1 ms apart, with an
**identical** GUID — measured on 8 of 11 outbound sends, and on 7 of 7 successful
`message/text` calls, i.e. exactly the hot path. `messages_provider_guid_uidx`
raises `23505` on the second copy. Every ingest path **catches that and reports
`deduped: true`**. It is the expected outcome, not an error.

---

## Setup

```bash
cd bridge
npm install

cp .env.example .env
# Fill in SUPABASE_SERVICE_ROLE_KEY:
supabase projects api-keys --project-ref yjtbjwnvbgriywbrhsez
# and a BRIDGE_API_TOKEN:
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

`bridge/.env` is gitignored. **`BB_PASSWORD` is not duplicated here** — it is read
from the repo-root `.env`, which owns it. One copy of that password, ever.

Requires a row in `senders` for `sender01` (slug, apple_email, bluebubbles_url,
bluebubbles_port, macos_user). `test/e2e.mjs` seeds it if it is missing.

## Running

```bash
npm run api      # gateway on 127.0.0.1:12350
npm run worker   # the send loop
npm run health   # sender health checker (add -- --once for a single sweep)

npm run check    # node --check every file
npm run e2e      # live end-to-end test (sends a real iMessage)
```

All three are long-running and handle `SIGTERM`/`SIGINT`. The worker finishes the
send in progress before exiting, so no message is ever abandoned in `sending`
with an unknown provider outcome.

Point BlueBubbles' webhook at `http://127.0.0.1:12350/v1/webhooks/bluebubbles`
(`scripts/bb.sh webhook-add <url>`).

---

## Endpoints

All `/v1/*` routes require `Authorization: Bearer $BRIDGE_API_TOKEN`, except the
webhook — BlueBubbles cannot be configured to send an Authorization header, and
the server binds loopback only.

### `POST /v1/messages`
```json
{ "to": "someone@example.com", "body": "hello", "sender_slug": "sender01" }
```
Normalizes the address, resolves or creates the contact and conversation
honouring sticky routing, runs `can_send()`, and enqueues.

- **202** — accepted for sending. Returns `id`, `state`, `temp_guid`. Never 200:
  nothing has been sent yet and this API refuses to imply otherwise.
- **403** — `can_send()` refused on policy: `suppressed:stop`,
  `no_consent_on_record`, `consent_revoked`, `sender_paused:sender01`, …
- **409** — `in_flight_message_exists` (FIFO), or `sticky_sender_mismatch`.

`sender_slug` only decides which sender a **new** contact is pinned to. An
existing contact keeps its sticky sender; naming a different one gets a 409, not
a reroute.

### `GET /v1/messages/:id`
State, all timestamps, `provider_guid`, `reconciled`, and the full `send_attempts`
history. Read the attempts as a story: `timeout → reconciled_sent` is a correctly
handled false failure; `timeout → accepted` is a **double-send**.

`state: "failed", reconciled: false` means the outcome is **unknown**, not that
the message failed.

### `GET /v1/contacts/:address`
Contact, sticky sender (with its current status), newest consent ledger row,
suppression, and a live `can_send()` verdict. Works on an un-normalized address.
Returns 404 with the suppression still attached if the address is suppressed but
has no contact row — people can opt out before we ever create a row for them.

### `POST /v1/suppressions`
```json
{ "address": "someone@example.com", "reason": "stop" }
```
`reason` ∈ `stop | dnc | bounce | manual`. Idempotent. **Global across every
sender and every conversation** — someone who said STOP to `sender01` has said
STOP to the operator.

### `POST /v1/consents`
```json
{ "address": "...", "status": "granted", "source": "web_form", "evidence": {} }
```
Not in the original spec, but `can_send()` refuses every contact with
`no_consent_on_record`, so without this the gateway can never send anything. The
ledger is append-only: a revocation is a new row, never an update.

### `POST /v1/webhooks/bluebubbles`
Ingests `{ type, data }`. Dedupes on `provider_guid` (catching the unique
violation), attaches GUIDs and `delivered_at` to outbound rows, records inbound
replies, and **auto-suppresses on STOP**.

STOP matching is an exact match on the whole trimmed message (`stop`,
`unsubscribe`, `cancel`, …), not a substring — "I'll stop by tomorrow" must not
opt someone out. A STOP writes both a suppression (what `can_send()` enforces)
and a `revoked` consent row (the audit trail of the person's decision), and is
honoured even from an address with no contact row.

### `GET /v1/health`
Per-sender status and queue depth. `queue.unreconciled_failed > 0` is the
Defect A triage queue — it must be empty before any retry sweep is safe.

---

## Worker behaviour

- Claims **one message per conversation** at a time.
  `messages_one_sending_per_conversation_uidx` enforces it; a lost race raises
  `23505`, which the worker catches and moves on from. Losing a claim is routine.
- Calls **`can_send_message()`** before every send and honours the verdict.
- Skips a paused sender. **Never reroutes** — its contacts simply stop receiving
  until it is fixed. That is the intended outcome, not a bug to route around.
- Ambiguous outcome → `failed` + `send_attempts` row → **reconcile** →
  `failed → sent` with `reconciled = true` if it landed.
- Rate limit: one message per **8–12 s per sender**, jittered. Deliberately slow.
  The constraint is Apple's tolerance for a young sender identity, not the machine.

### Why `can_send_message()` exists

`can_send(contact_id)` refuses whenever anything is queued or sending on the
conversation — which includes the message the worker is about to send. So
`can_send()` alone can **never** approve a real send: a worker honouring it
literally would send nothing, and one ignoring `in_flight_message_exists` would
be bypassing the gate.

`can_send_message(message_id)` (migration `20260915180000`) resolves this without
weakening anything. It **calls `can_send()`** for checks 0–4 — contact exists,
global suppression, consent, sender status, sticky-sender match — and does not
duplicate a line of that logic, so it cannot drift. It then re-evaluates *only*
check 5 with the target message excluded, requiring it to be the FIFO head: no
other message `sending`, none `queued` ahead of it. Every other refusal is passed
through unchanged. Like `can_send()`, it takes no sender override.

### Unreachable addresses

An address not registered with Apple hangs 90–100 s and comes back `error=22`.
There is **no reachability oracle** without the Private API —
`GET /handle/:address` reads the local `chat.db` and 404s for anyone never
contacted from this Mac, which is every new recipient. So it is used as an
advisory hint only.

Instead the worker pays that cost **once per address**: an `error=22` writes a
`bounce` suppression, and because suppression is `can_send()`'s first check,
every subsequent message to that address is refused at enqueue time in
microseconds. One bad address can stall the queue for 90 s exactly once, ever.

### Health checker

`senders.status` can read `active` while the BlueBubbles instance is dead — this
actually happened after a reboot, because no auto-login is configured and the Mac
sits at a login window until a human arrives. `worker/health.mjs` pings each
sender and flips `status` to `failed` (with a `paused_reason`) after
`HEALTH_FAIL_THRESHOLD` consecutive failures, and back to `active` on recovery.

It only un-pauses senders **it** paused (`paused_reason` starting `healthcheck:`).
A sender an operator disabled by hand is left alone — a health checker that
reactivates a deliberately disabled sender is worse than no health checker.

---

## Reconciliation, honestly

`reconcileByTempGuid(tempGuid, text, …)` decides whether an ambiguous send landed.

**BlueBubbles does not persist `tempGuid`.** It is a client correlation token the
server echoes on the webhook but never writes to `chat.db`, so provider state
cannot be queried by it. The documented fallback — *text + chat + time window* —
is in fact the only available lookup, and the function reports
`confidence: 'text_window'` rather than claiming certainty. Two identical bodies
sent to the same chat inside the window would be indistinguishable. The window is
anchored to the attempt's start time to keep that as tight as possible.

The third outcome matters as much as the other two:

| Result | Action |
|---|---|
| found in provider state | `failed → sent`, `reconciled = true`, GUID attached, `reconciled_sent` attempt logged |
| not found | `reconciled = true` only; row stays `failed` and is now legally requeueable |
| **provider state unreadable** | `reconciled` stays **false** — "we could not check" is not "it did not deliver" |

That third row is why `messages_unreconciled_failed_idx` exists. Requeueing is
left to an operator or a retry sweep rather than done inline, so a genuinely
broken address cannot spin.

---

## Test results (2026-09-15, live)

`node test/e2e.mjs` — **16/16 passed** against the real Supabase project, the real
BlueBubbles instance, and a real iMessage to `meidy765@gmail.com`.

| | |
|---|---|
| Suppressed address | `can_send` → `suppressed:stop`, 403, zero rows enqueued |
| Duplicate webhook, identical GUID | second call `deduped: true`, exactly **1** row |
| Unauthenticated request | 401 |
| **Message 1** (`chat/new`) | HTTP **500 after 120.0 s** → reconciled → `sent`, `reconciled=true`, GUID `E81C8130…`, attempts `error → reconciled_sent`. Chat GUID `any;-;meidy765@gmail.com` recovered and stored. |
| **Message 2** (`message/text`) | HTTP **200 in 697 ms** → `sent`, GUID `7E71E195…`, attempts `accepted` |
| Provider state | both messages `error=0`, `isDelivered=true`, GUIDs match the DB exactly |

Separately verified: the health checker flips `sender02` → `failed` after 2 failed
pings and back to `active` on recovery; and in a rolled-back transaction,
`can_send_message()` returns `ok` for the FIFO head, `in_flight_message_exists`
for the message behind it, `sender_paused:sender01` when the sender is paused and
`suppressed:stop` when suppressed.

Test rows were deleted afterwards. The `sender01` row is real configuration and
was left in place.

**Not exercised against live traffic:** a real inbound reply through the webhook
(inbound generation is blocked by a TCC denial — see Phase 1 Note 2; the path was
tested with a synthetic payload), and the retry sweep after a *genuinely
undelivered* send, which needs a send that fails without delivering.
