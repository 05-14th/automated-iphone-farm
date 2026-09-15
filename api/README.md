# api — Python REST interface to the iMessage bridge

A FastAPI service that other systems call to send and read iMessages through the
Mac-mini sender farm. It is designed to run on a **separate, public server**.

Read `docs/PHASE1-REPORT.md` and `docs/BRIDGE-SCHEMA.md` first. Everything below
is a consequence of what those two documents measured.

---

## 1. The architecture, and why it is shaped like this

BlueBubbles listens on **`127.0.0.1:12341` on the Mac mini** and nothing else.
That is deliberate and it is not going to change: exposing an unauthenticated-ish
AppleScript bridge that drives a signed-in Apple Account to the internet would put
the sender identity, and every conversation on it, one port scan away.

So this API **cannot** talk to BlueBubbles, and never tries to. Instead:

```
        PUBLIC SERVER                      SUPABASE                    MAC MINI (private)
 ┌───────────────────────┐          ┌──────────────────┐        ┌────────────────────────────┐
 │                       │  writes  │                  │ claims │                            │
 │   Python API (this)   ├─────────▶│    messages      │◀───────┤  Node worker (bridge/)     │
 │   FastAPI + uvicorn   │          │    contacts      │        │        │                   │
 │                       │  reads   │    conversations │ writes │        ▼                   │
 │   • validates         │◀─────────┤    consents      │◀───────┤  BlueBubbles 127.0.0.1:12341│
 │   • gates (can_send)  │          │    suppressions  │        │        │                   │
 │   • ENQUEUES          │          │    send_attempts │        │        ▼                   │
 │                       │          │                  │        │  Messages.app (imsg01)     │
 │   NEVER sends         │          │  system of record│        │        │                   │
 └───────────┬───────────┘          └──────────────────┘        └────────┼───────────────────┘
             │                               ▲                           │
   HTTPS ────┘                               │  webhook ingest           ▼
   caller / your app                         └───── Node gateway ◀── inbound reply
                                                    (also on the Mac,
                                                     127.0.0.1:12350)

     ── no arrow exists, or may ever exist, from the Python API to BlueBubbles ──
```

**The two processes meet in the database and nowhere else.** That is the whole
design:

| | Python API (this) | Node worker (`bridge/`) |
|---|---|---|
| Runs on | a public server, anywhere | the Mac mini, next to Messages.app |
| Talks to | Supabase only | Supabase **and** BlueBubbles on loopback |
| Sends messages | **never** | yes — it is the only thing that does |
| Public | yes, behind TLS + a bearer token | no, loopback only |

### What follows from that

* `POST /v1/messages` returns **202 Accepted**, never 200. Nothing has been sent
  at that point; a row has been queued.
* This API cannot tell you whether the Mac is healthy. `GET /v1/health` reports
  what the *database* says — a sender row can read `active` while its BlueBubbles
  instance is dead. The Node health checker on the Mac is what catches that.
* Throughput is not a property of this API. The worker paces sends at **one
  message every 8–12 seconds per sender**, on purpose.

---

## 2. Install

Python 3.12+ (developed and tested on 3.12.13).

```bash
cd api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# then fill in .env — see below
```

`.env` and `.venv/` are gitignored. Nothing secret is ever committed.

### Environment

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `SUPABASE_URL` | yes | — | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | — | `service_role` key. BYPASSRLS. Server-side only; never ships to a browser. |
| `API_TOKEN` | yes | — | Bearer token for every `/v1/*` route. Minimum 16 chars. |
| `API_HOST` | no | `127.0.0.1` | Bind address. Keep it loopback and put a TLS proxy in front. |
| `API_PORT` | no | `8080` | |
| `DEFAULT_SENDER_SLUG` | no | `sender01` | Which sender a **brand new** contact is pinned to when the caller names none. |
| `LOG_LEVEL` | no | `info` | |
| `DOCS_ENABLED` | no | `true` | Serve `/docs` and `/redoc`. |
| `CORS_ORIGINS` | no | *(empty — CORS off)* | Comma-separated origins. A browser should not be holding `API_TOKEN`. |
| `DB_TIMEOUT_SECONDS` | no | `20` | PostgREST client timeout. |

Generate the two secrets:

```bash
supabase projects api-keys --project-ref yjtbjwnvbgriywbrhsez   # SUPABASE_SERVICE_ROLE_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"        # API_TOKEN
```

## 3. Run

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Interactive docs: <http://127.0.0.1:8080/docs> · <http://127.0.0.1:8080/redoc>

Shutdown is graceful: uvicorn stops accepting, drains in-flight requests, then
the lifespan handler closes the HTTP pool. Nothing can be lost — this process
never holds a send.

For a real deployment (systemd, TLS, firewall) see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## 4. Auth

Every route requires `Authorization: Bearer $API_TOKEN` **except** `/v1/health`,
`/health`, `/docs`, `/redoc` and `/openapi.json`.

The comparison uses `secrets.compare_digest` over SHA-256 digests, so neither the
token's value nor its length leaks through timing.

```bash
curl -s http://127.0.0.1:8080/v1/messages
# {"error":{"code":"unauthorized","message":"A valid bearer token is required."}}
```

There is one shared token, so the API cannot prove *who* is calling. Where that
matters — deleting a suppression — send an `X-Actor:` header; it is recorded as a
**claim** alongside the source IP, never as proof.

### Error envelope

Every non-2xx response has exactly this shape, and never contains a stack trace,
the Supabase URL or the service-role key:

```json
{"error": {"code": "refused", "message": "Refused by the pre-send gate: suppressed:stop",
           "detail": {"reason": "suppressed:stop", "contact_id": "…"}}}
```

---

## 5. Endpoints

Examples below use `T=$API_TOKEN` and `B=http://127.0.0.1:8080`. **Every response
shown is real output captured from the running service.**

### `POST /v1/messages` — enqueue one message

```bash
curl -s -X POST $B/v1/messages -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"to":"meidy765@gmail.com","body":"quick mode test"}'
```

```json
{
  "id": "16771a4c-a378-4107-8e11-f42f12fbfb0e",
  "state": "queued",
  "mode": "quick",
  "scheduled_for": null,
  "conversation_id": "0f73a7ed-59aa-4bb1-913d-189826d91856",
  "contact_id": "baad064f-306e-4ec7-b0bf-a9110bfac40d",
  "sender_slug": "sender01",
  "normalized_address": "meidy765@gmail.com",
  "temp_guid": "temp-b0f9098a-d67e-4dd7-8cfc-fa8daf230345",
  "queued_at": "2026-09-15T18:44:00.746760Z"
}
```
→ **HTTP 202.**

What happened, in order: the address was normalized; the contact was resolved or
created (and pinned); the conversation was resolved; `can_send()` was consulted;
a `queued` row was written with a `temp_guid` for Defect-A reconciliation.

**Scheduled:**

```bash
curl -s -X POST $B/v1/messages -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"to":"+17783231234","body":"scheduled test","mode":"scheduled","send_at":"2030-01-01T17:00:00Z"}'
```

```json
{"id":"d6d69366-…","state":"queued","mode":"scheduled","scheduled_for":"2030-01-01T17:00:00Z", …}
```

**Refusals:**

| Situation | Status | Body |
|---|---|---|
| No consent on record | 403 | `{"error":{"code":"refused","message":"Refused by the pre-send gate: no_consent_on_record","detail":{"reason":"no_consent_on_record", …}}}` |
| Suppressed | 403 | `…"reason":"suppressed:stop"` |
| Sender paused/failed | 403 | `…"reason":"sender_paused:sender01"` |
| A message is already queued/sending on that conversation | 409 | `{"error":{"code":"in_flight_message_exists", …}}` |
| Caller named a sender the contact is not pinned to | 409 | `{"error":{"code":"sticky_sender_mismatch", …}}` |
| `mode=scheduled` with no/past `send_at` | 422 | `{"error":{"code":"validation_error", …}}` |

`sender_slug` decides only where a **new** contact is pinned. An existing contact
keeps its sticky sender forever; naming a different one is a 409, never a
reroute. A rerouted contact would see the conversation continue from a stranger's
address — the single worst trust failure available in this channel.

### `POST /v1/messages/bulk` — up to 100 at once

Each item is resolved and gated independently. **One refusal never aborts the
batch.** The HTTP status is always 200; read `results[].accepted`.

```bash
curl -s -X POST $B/v1/messages/bulk -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"mode":"quick","messages":[
    {"to":"alice@example.com","body":"bulk one"},
    {"to":"alice@example.com","body":"bulk two"},
    {"to":"bob@example.com","body":"bulk three"}]}'
```

```json
{
  "accepted_count": 1,
  "refused_count": 2,
  "results": [
    {"index": 0, "to": "alice@example.com", "accepted": true,
     "message": {"id": "a64f863b-…", "state": "queued", "mode": "quick", "temp_guid": "temp-…", …},
     "error": null},
    {"index": 1, "to": "alice@example.com", "accepted": false, "message": null,
     "error": {"code": "in_flight_message_exists",
               "message": "Another message is already queued or sending on this conversation. …",
               "detail": {"reason": "in_flight_message_exists", …}}},
    {"index": 2, "to": "bob@example.com", "accepted": false, "message": null,
     "error": {"code": "refused", "message": "Refused by the pre-send gate: no_consent_on_record", …}}
  ]
}
```

Note item 1: **two messages to the same contact in one batch** will see the second
refused, because the first is now queued ahead of it. That is per-conversation
FIFO working correctly, not a bug. Send the follow-up after the first clears.

`mode` and `send_at` apply to the whole batch.

### `DELETE /v1/messages/{id}` — cancel

Only while the message is still `queued` and unclaimed.

```json
{"id":"16771a4c-…","state":"failed","cancelled":true,
 "message":"Cancelled before pickup. Nothing was handed to BlueBubbles."}
```

Once the worker has claimed it (`sending`) there is nothing to cancel — BlueBubbles
already has the text, and per Defect A the HTTP response would not tell us whether
it went out anyway:

```json
{"error":{"code":"not_cancellable",
  "message":"Message is 'sending', not 'queued'. Only an unclaimed message can be cancelled.",
  "detail":{"id":"d6d69366-…","state":"sending"}}}
```
→ **HTTP 409.**

A cancelled row becomes `state=failed`, `error_code=cancelled_by_api`,
`reconciled=true`. `reconciled=true` is honest and load-bearing: nothing was ever
handed to the provider, so there is no ambiguous outcome to resolve, and the row
must not sit in the Defect A triage queue pretending there is.

### `GET /v1/messages/{id}` — full state + attempts

```json
{
  "id": "16771a4c-…", "conversation_id": "0f73a7ed-…", "sender_id": "7058aae9-…",
  "direction": "outbound", "state": "queued", "body": "quick mode test",
  "provider_guid": null, "temp_guid": "temp-b0f9098a-…", "error_code": null,
  "reconciled": false, "scheduled_for": null,
  "queued_at": "2026-09-15T18:44:00.746760Z", "sending_at": null, "sent_at": null,
  "delivered_at": null, "failed_at": null, "read_at": null,
  "created_at": "2026-09-15T18:44:00.746760Z",
  "attempts": []
}
```

Read `attempts` as a story: `timeout → reconciled_sent` is a correctly handled
false failure; `timeout → accepted` is a **double-send**.

> `"state": "failed", "reconciled": false` means the outcome is **unknown**, not
> that the message failed. BlueBubbles returns 500 / times out on sends that
> actually delivered.

### `GET /v1/messages` — list

Filters: `state`, `direction`, `contact` (any format — normalized before lookup),
`sender_slug`, `conversation_id`, `since`, `until`. Pagination: `cursor` (keyset,
stable) or `offset`; `limit` default 50, max 200.

```bash
curl -s "$B/v1/messages?state=failed&sender_slug=sender01&limit=5" -H "Authorization: Bearer $T"
```

```json
{"count": 2, "next_cursor": null, "messages": [ { … }, { … } ]}
```

When a page is full, `next_cursor` carries the oldest `created_at` on it; pass it
back as `?cursor=` for the next page.

### `GET /v1/inbox` — received messages

Inbound only, newest first. Filters: `since`, `until`, `contact`, `unread_only`,
plus the same pagination.

```bash
curl -s "$B/v1/inbox?unread_only=true&limit=50" -H "Authorization: Bearer $T"
```

```json
{"count": 1, "next_cursor": null, "messages": [
  {"id":"…","direction":"inbound","state":"sent","body":"Hi","provider_guid":"E81C8130-…",
   "temp_guid":null,"read_at":null,"created_at":"2026-09-15T18:52:03Z", …}]}
```

Inbound rows are observed facts: already `sent`, already carrying a
`provider_guid`, never a `temp_guid`. Duplicate webhooks (Defect B) are collapsed
by a unique index on the Mac before they ever reach here.

`POST /v1/inbox/{id}/read` sets `read_at`. It is operator-facing metadata only —
it has no effect on sending or on the consent gate.

### `GET /v1/conversations` · `GET /v1/conversations/{id}/messages`

```json
{"count": 3, "conversations": [
  {"id":"802bfed3-…","contact_id":"38f4c7b3-…","sender_id":"7058aae9-…",
   "provider_chat_guid":null,"address":"someone@example.com","display_name":null,
   "created_at":"2026-09-15T18:47:05.748271Z"}]}
```

`provider_chat_guid: null` does **not** mean no chat exists on the provider —
`chat/new` can create the chat and still fail to return its id.

### `GET /v1/contacts/{address}`

Accepts an un-normalized address. `(778) 323-1234`, `MEIDY765@Gmail.com` and
`+17783231234` all resolve correctly.

```json
{
  "id": "baad064f-…", "normalized_address": "someone@example.com",
  "raw_address": "Someone@Example.com", "display_name": null,
  "sticky_sender": {"id":"7058aae9-…","slug":"sender01","status":"active",
                    "paused_reason":null,"apple_email":"meidy@clariocapital.com"},
  "consent": {"id":"312f9070-…","status":"granted","source":"web_form",
              "evidence":{"test":true},"granted_at":"2026-09-15T18:43:52.617421Z",
              "revoked_at":null,"created_at":"2026-09-15T18:43:52.674868Z"},
  "suppression": null,
  "can_send": {"allowed": true, "reason": "ok"},
  "created_at": "2026-09-15T18:43:52.105114Z"
}
```

If the address has no contact row **but is suppressed**, the 404 still carries the
suppression — people can opt out before we ever create a row for them, and hiding
that would be the most dangerous possible 404:

```json
{"error":{"code":"not_found","message":"No contact row for this address.",
  "detail":{"normalized_address":"someone@example.com",
            "suppression":{"reason":"stop","created_at":"2026-09-15T18:47:04.921147+00:00"},
            "note":"Suppressed with no contact row - an opt-out recorded before we ever messaged this address. It will be refused at enqueue time."}}}
```

### `POST /v1/consents`

`can_send()` refuses every contact with `no_consent_on_record`, so this call is
the prerequisite for sending to anyone.

```bash
curl -s -X POST $B/v1/consents -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"address":"someone@example.com","status":"granted","source":"web_form","evidence":{"form_id":"lead-2026-09"}}'
```

```json
{"contact_id":"baad064f-…","normalized_address":"someone@example.com",
 "consent":{"id":"312f9070-…","status":"granted","source":"web_form",
            "evidence":{"form_id":"lead-2026-09"},"granted_at":"2026-09-15T18:43:52.617421Z",
            "revoked_at":null,"created_at":"2026-09-15T18:43:52.674868Z"},
 "can_send":{"allowed":true,"reason":"ok"}}
```
→ **HTTP 201.**

The ledger is **append-only**: a revocation is a new row with
`status: "revoked"`, never an update. `can_send()` reads the newest row.

### `GET` / `POST` / `DELETE /v1/suppressions`

Suppression is **global across every sender**. Someone who said STOP to
`sender01` has said STOP to the operator. It is `can_send()`'s first check and it
outranks consent.

```bash
curl -s -X POST $B/v1/suppressions -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"address":"API-Test@Example.COM","reason":"stop"}'
```

```json
{"suppression":{"id":"6aa787b6-…","normalized_address":"api-test@example.com",
  "raw_address":"API-Test@Example.COM","reason":"stop",
  "created_at":"2026-09-15T18:47:04.921147Z"},"created":true}
```

Idempotent — a second STOP returns `"created": false`. Note the normalization:
that is what makes suppression actually work.

`reason` ∈ `stop` | `dnc` | `bounce` | `manual`. The worker writes `bounce`
itself when Apple returns `error=22`.

**Deleting a suppression is audited**, always:

```bash
curl -s -X DELETE "$B/v1/suppressions/api-test@example.com?note=verified+opt-in" \
  -H "Authorization: Bearer $T" -H "X-Actor: ops@example.com"
```

```json
{"deleted":{"id":"6aa787b6-…","normalized_address":"api-test@example.com",
            "raw_address":"API-Test@Example.COM","reason":"stop",
            "created_at":"2026-09-15T18:47:04.921147Z"},
 "audit":{"deleted_by":"ops@example.com","deleted_at":"2026-09-15T18:47:06.123331Z",
          "source_ip":"127.0.0.1","note":"api integration test",
          "recorded_in":"public.suppression_deletions"}}
```

The audit row is written to `public.suppression_deletions` **before** the delete,
so a crash between the two leaves an over-recorded trail rather than a silent
un-suppression. Nothing ever updates or deletes rows in that table.

Deleting a `stop` suppression does **not** restore consent — the ledger is
separate, and `can_send()` will still refuse with `consent_revoked` if the person
opted out by replying STOP.

### `GET /v1/health` — no auth

```json
{"status":"ok","database":true,
 "senders":[{"id":"7058aae9-…","slug":"sender01","status":"active",
             "paused_reason":null,"apple_email":"meidy@clariocapital.com"}],
 "queue":{"queued":0,"scheduled":0,"sending":0,"unreconciled_failed":0},
 "version":"1.0.0","checked_at":"2026-09-15T18:43:33.838671Z","note":null}
```

* `queued` — outbound and **due now**
* `scheduled` — outbound, queued, not yet due
* `sending` — claimed by the worker, in flight
* `unreconciled_failed` — **the Defect A triage queue**. Non-zero ⇒ some messages
  have an unknown real outcome, `status` becomes `degraded`, and no retry sweep
  is safe until it is zero.

`degraded` still returns **200** with an `X-Health: degraded` header. It is
information, not a reason for a load balancer to pull the host; 503 is reserved
for "this process cannot serve".

### `GET /v1/stats`

```json
{"since":"2026-09-14T18:47:17.644303Z","until":"2026-09-15T18:47:17.644303Z",
 "outbound":{"queued":0,"sending":1,"sent":2,"failed":2},
 "inbound":{"total":0,"unread":0},
 "totals":{"outbound":5,"inbound":0,"all":5,"unreconciled_failed":0,
           "queued_including_scheduled":0}}
```

### Endpoints added beyond the brief, and why

| Endpoint | Justification |
|---|---|
| `POST /v1/inbox/{id}/read` | `GET /v1/inbox` was specified with `unread_only` *"if you add a read flag"*. A read flag with no way to set it would be decorative. |
| `GET /v1/health` (alias `/health`) | Same handler on both paths so a load balancer's default probe path works without configuration. `/health` is hidden from the schema. |

---

## 6. Scheduled vs quick

|  | `quick` (default) | `scheduled` |
|---|---|---|
| Body | `{"to":…, "body":…}` | `+ "mode":"scheduled", "send_at":"2030-01-01T17:00:00Z"` |
| `send_at` | must be **absent** (422 if present — silently ignoring it would be worse) | required, must carry an offset, must be in the future |
| Stored as | `scheduled_for = NULL` | `scheduled_for = <send_at, as UTC>` |
| State | `queued` | `queued` |
| Picked up | next worker tick | first tick at or after `scheduled_for` |

**`scheduled` is not a state.** A scheduled message is genuinely `queued`, just
not yet due. Keeping it in `queued` means the send state machine, the
per-conversation FIFO index and the Defect A reconciliation rules all continue to
apply to it unchanged — a fifth state would have needed every one of those
re-derived.

### The consequence you need to know about

`can_send()` refuses while **anything** is queued on the conversation. So a
message scheduled for next Tuesday **blocks every other message to that contact
until it goes out** — subsequent sends get `409 in_flight_message_exists`.

That is the FIFO rule applying honestly, and this API does not weaken it. If you
need to schedule far ahead and still send in the meantime, hold the schedule in
your own system and `POST` when it is due.

---

## 7. REQUIRED Node worker change

**The Node worker does not know about `scheduled_for` and will send a scheduled
message immediately.** This was confirmed live during testing on 2026-09-15: a
message scheduled for 2030 was claimed within seconds by the worker that was
already running on this Mac.

Migration `20260915210000_phase13_scheduled_send_and_audit.sql` is written so a
`NULL` `scheduled_for` keeps today's behaviour **exactly** — no default changed,
no constraint tightened, no index dropped, no backfill needed. Quick sends are
unaffected. Only scheduled sends need the change below.

### The change — one line in `bridge/lib/db.mjs`

In `claimableMessages()`:

```js
export async function claimableMessages(senderId, limit = 25) {
  const { data, error } = await db
    .from('messages')
    .select('*, conversations!inner(id, provider_chat_guid, contact_id)')
    .eq('sender_id', senderId)
    .eq('state', 'queued')
    .eq('direction', 'outbound')
    .or(`scheduled_for.is.null,scheduled_for.lte.${new Date().toISOString()}`)   // <-- ADD THIS LINE
    .order('queued_at', { ascending: true })
    .limit(limit);
  throwIf(error, 'claimableMessages');
  return data ?? [];
}
```

Semantics: `scheduled_for IS NULL OR scheduled_for <= now()`. `NULL` is unchanged
in every respect, so the worker's behaviour for existing traffic is identical.

Two notes:

* The timestamp must be computed **per call**, inside the function — a value
  captured at module load would freeze and the worker would eventually send
  nothing new.
* This is deliberately *not* applied here. `bridge/` is out of scope for this
  work; the change is stated precisely so it can be made, reviewed and tested as
  its own commit.

Until it is made, treat `mode=scheduled` as **not yet functional end to end**.
It is fully functional at the API and database level — the row carries the right
time — but the worker will ignore it.

An optional second change, once the first is in: `worker/worker.mjs` could log
the count of not-yet-due messages it skipped, so an operator can see the schedule
backlog from the worker's own logs rather than only from `GET /v1/health`.

---

## 8. Rate limits and pacing

The Mac paces sends at **one message per 8–12 seconds per sender**, jittered
(`WORKER_MIN_INTERVAL_MS` / `WORKER_MAX_INTERVAL_MS` in `bridge/.env`). The
constraint is Apple's tolerance for a young sender identity, not the machine.

With one sender that is roughly:

| Window | Ceiling |
|---|---|
| per minute | ~5–7 messages |
| per hour | ~300–450 |
| per day | ~7,200–10,800 (theoretical; nobody should run a young identity at that rate) |

**This API does not promise any of that.** It accepts as fast as you can post and
the queue absorbs the difference. `POST /v1/messages/bulk` with 100 messages is
about 13–20 minutes of actual sending on one sender. Plan against the pacing, not
against this API's response time.

The first message of any conversation additionally costs **75–120 seconds** on the
Mac, because `chat/new` always times out before it returns (Defect A). The worker
handles it and reconciles; but a batch of 100 *new* contacts is slow in a way a
batch of 100 *existing* conversations is not.

There is no rate limiting **in** this API. If you expose it beyond a trusted
caller, rate-limit at the reverse proxy — see DEPLOYMENT.md.

---

## 9. Tests

```bash
.venv/bin/python -m pytest tests -q                 # everything (~7 s)
.venv/bin/python -m pytest tests -m "not live" -q   # unit only, no network
.venv/bin/python -m pytest tests -m live -q         # the live Supabase test only
```

Last run on this Mac, 2026-09-15: **74 passed in 7.11 s.**

| File | Covers |
|---|---|
| `test_normalize.py` | Address normalization against the SQL function's own worked examples; the case/format-collapse property suppression depends on. |
| `test_auth.py` | Constant-time token comparison, 401 on all 13 protected routes, open routes, no token in any response body. |
| `test_validation.py` | Scheduling rules (missing/past/naive `send_at`, `send_at` on a quick send), bulk size caps, sender-slug pattern. |
| `test_endpoints.py` | 202-not-200, gate refusals as 403 vs 409, sticky mismatch, bulk partial failure, cancel semantics, uniform error envelope, and that every route has a summary and description. |
| `test_integration_live.py` | The real Supabase project, end to end. |

The live test creates real rows and **deletes everything it created** in a
`finally` block — it asserts the cleanup afterwards. It uses synthetic
`@example.com` addresses (RFC 2606 reserved, not registered with Apple), so even
if the running worker claims a row, no message can reach a person. It never
starts the worker and never calls BlueBubbles.

---

## 10. Troubleshooting

**`422 validation_error` on every send.** `to` must look like an email or
normalize to E.164. `not-an-address` and bare `123` are rejected before any
database round trip.

**`403 no_consent_on_record`.** Expected for any address with no consent row.
`POST /v1/consents` first. This is `can_send()` doing its job.

**`403 sender_paused:sender01`.** The sender's row is not `active`. Either the
health checker on the Mac paused it (`paused_reason` starts `healthcheck:`) or an
operator did. Nothing is rerouted — by design. Fix the sender.

**`409 in_flight_message_exists` and it will not clear.** Something is stuck
`queued` or `sending` on that conversation. Check `GET /v1/health`: if
`sending > 0` and it never moves, the worker is wedged or the Mac is at a login
window. If `unreconciled_failed > 0`, that is Defect A triage — do **not** retry
anything until it is zero.

**Everything enqueues but nothing ever sends.** This API cannot tell you why; it
has no route to the Mac. Check, on the Mac: is `imsg01` logged in (a reboot
leaves the sender dead until a human logs in — no auto-login is configured), is
`worker/worker.mjs` running, is BlueBubbles up on 12341.

**A scheduled message went out immediately.** The worker change in §7 has not
been made.

**`502 database_error`.** Supabase rejected the write. `detail.pg_code` carries
the Postgres SQLSTATE: `23505` is a unique violation (usually a duplicate
`provider_guid` — Defect B, harmless), `23514` a check-constraint or trigger
refusal (a sticky-routing or state-machine violation — read it, do not work
around it).

**`503` at startup.** `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is missing or
wrong. The process refuses to start rather than run insecurely.

**`GET /v1/health` says `ok` but nothing is being delivered.** Expected and
documented: a sender row can read `active` while its BlueBubbles instance is
dead. This endpoint reports the database's view. The Mac's own health checker is
what flips the row.
