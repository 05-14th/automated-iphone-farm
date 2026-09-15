# Phase 13 — the Python API, scheduled sending, and the audit trail

New document. `docs/BRIDGE-SCHEMA.md` is unchanged and remains the authority on
the Phase 11 data model; this one covers only what Phase 13 adds on top:

| | |
|---|---|
| New service | `api/` — a Python (FastAPI) REST interface, deployed on a **separate server** |
| New migration | `supabase/migrations/20260915210000_phase13_scheduled_send_and_audit.sql` |
| New columns | `messages.scheduled_for`, `messages.read_at` |
| New table | `public.suppression_deletions` |
| Required change elsewhere | **one line** in `bridge/lib/db.mjs` (§4). Not made yet. |

---

## 1. Two processes, one database

```
            PUBLIC SERVER                    SUPABASE                     MAC MINI (private)
   ┌────────────────────────────┐      ┌──────────────────┐      ┌──────────────────────────────┐
   │  api/  (Python, FastAPI)   │      │                  │      │  bridge/worker/worker.mjs    │
   │                            │      │   messages       │      │            │                 │
   │  validates                 │─────▶│   contacts       │◀─────│            ▼                 │
   │  gates via can_send()      │      │   conversations  │      │  BlueBubbles 127.0.0.1:12341 │
   │  ENQUEUES                  │◀─────│   consents       │─────▶│            │                 │
   │                            │      │   suppressions   │      │            ▼                 │
   │  never sends               │      │   send_attempts  │      │  Messages.app (imsg01)       │
   │  never touches BlueBubbles │      │                  │      │            │                 │
   └────────────┬───────────────┘      │ system of record │      │            ▼                 │
                │                      └──────────────────┘      │      recipient               │
         HTTPS  │                               ▲                │            │                 │
        caller ─┘                               │                │  bridge/api/server.mjs ◀─────┘
                                                └────────────────┤  (loopback webhook ingest)
                                                                 └──────────────────────────────┘
```

BlueBubbles listens on `127.0.0.1:12341` on the Mac mini and is deliberately not
exposed. It stays that way: anything that reaches it can read every conversation
on a signed-in Apple Account and send as that identity, behind nothing but a
shared password over plain HTTP.

The consequence is the whole design. The Python API is the public interface and
it writes to Supabase. The Node worker stays on the Mac and is the only process
that touches BlueBubbles. **They meet in the database and nowhere else.** There
is no arrow from the Python API to BlueBubbles, and there must never be one.

Secondary consequences worth stating:

* The API returns **202**, never 200, on a send. Nothing has left the Mac.
* The API cannot observe the Mac. `GET /v1/health` reports the database's view;
  a sender row can read `active` while its BlueBubbles instance is dead.
* Inbound messages reach the database through the Node gateway's webhook on the
  Mac's loopback. The Python API has no webhook — BlueBubbles could not reach it.

---

## 2. `messages.scheduled_for`

```sql
alter table public.messages add column if not exists scheduled_for timestamptz;

create index messages_scheduled_due_idx
  on public.messages (sender_id, scheduled_for)
  where state = 'queued' and direction = 'outbound' and scheduled_for is not null;

alter table public.messages add constraint messages_scheduled_outbound_chk
  check (scheduled_for is null or direction = 'outbound');
```

**`NULL` means "due immediately", which is exactly today's behaviour.** Every row
written before this column existed, and every `mode=quick` send, has `NULL`. No
default changed, no constraint tightened, no index dropped, no backfill needed.

### Scheduled is not a state

A scheduled message sits in `state = 'queued'`. It is genuinely queued — just not
yet due. A fifth state would have required the send state machine
(`trg_messages_state_machine`), the FIFO index
(`messages_one_sending_per_conversation_uidx`), `can_send()` and the whole Defect
A reconciliation story to be re-derived around it. A due *time* changes none of
them.

The index is partial on `scheduled_for is not null`, because the overwhelming
majority of rows will never be scheduled and `messages_queued_outbound_idx`
already serves those.

### The FIFO consequence, stated plainly

`can_send()` check 5 refuses while anything is `queued` or `sending` on the
conversation. A message scheduled for next Tuesday therefore **blocks every other
message to that contact until it goes out**; the API returns `409
in_flight_message_exists` for the rest.

That is the FIFO rule applying honestly and the API does not weaken it. A caller
that needs to schedule far ahead and still send in the meantime should hold the
schedule in its own system and post when due. Changing `can_send()` to ignore
not-yet-due messages would mean a scheduled message could be overtaken by a later
quick one — out-of-order delivery in a thread a human is reading.

---

## 3. `messages.read_at` and `suppression_deletions`

**`read_at`** — nullable, inbound-only (`messages_read_inbound_chk`), with a
partial index for the unread inbox. Operator-facing metadata: it has no effect on
sending, on `can_send()`, or on any trigger. `NULL` for every pre-existing row.

**`suppression_deletions`** — append-only audit of every removal from the
suppression list. Deleting a suppression makes a person who opted out reachable
again; it is the single most consequential write the API offers. The audit row is
inserted **before** the delete, so a crash between the two leaves an
over-recorded trail rather than a silent un-suppression. RLS enabled, deny-all
policy, grants revoked from `anon` and `authenticated`, exactly as the other
seven tables.

`deleted_by` holds the caller's claimed identity (the `X-Actor` header), recorded
alongside `source_ip`. The API authenticates one shared bearer token, so it is a
**claim**, not proof — and the column comment says so, because a future reader
will otherwise assume otherwise.

---

## 4. REQUIRED change to the Node worker

**`bridge/worker/worker.mjs` does not know about `scheduled_for` and will send a
scheduled message immediately.**

This is not theoretical. On 2026-09-15, during testing of the Python API, a
message scheduled for 2030-01-01 was claimed within seconds by the worker already
running on this Mac, went `queued → sending`, and came back `failed` with
`chatnew:http:500` after 105 s (the address was a synthetic `@example.com` one, so
nothing reached a person).

The change is one line, in `bridge/lib/db.mjs`, in `claimableMessages()`:

```js
  .eq('state', 'queued')
  .eq('direction', 'outbound')
  .or(`scheduled_for.is.null,scheduled_for.lte.${new Date().toISOString()}`)   // <-- ADD
  .order('queued_at', { ascending: true })
```

i.e. `scheduled_for IS NULL OR scheduled_for <= now()`.

* `NULL` behaviour is unchanged in every respect, so existing traffic is
  unaffected.
* The timestamp must be computed **per call**, inside the function. A value
  captured at module load would freeze, and the worker would eventually stop
  picking up anything new.

It is deliberately not applied as part of this work: `bridge/` was out of scope,
and the change deserves its own commit, review and test. **Until it is made,
`mode=scheduled` is not functional end to end** — the row carries the right time,
and the worker ignores it.

---

## 5. Verification performed

Migration applied with `supabase db push` on 2026-09-15 against project
`yjtbjwnvbgriywbrhsez`; `supabase migration list` shows local and remote in
agreement.

The Python API was run locally against the live database and every endpoint
exercised over real HTTP. Full results are in `api/README.md`; the parts that
matter here:

| Check | Result |
|---|---|
| `scheduled_for` round-trips through the API and lands on the row | pass |
| A scheduled message is `state=queued`, not a new state | pass |
| `can_send()` refuses a second message on the same conversation | pass (`409 in_flight_message_exists`) |
| Cancel of a queued message → `failed` / `cancelled_by_api` / `reconciled=true` | pass |
| Cancel of a claimed message | refused, `409 not_cancellable` |
| `read_at` set and `unread_only` honoured | pass |
| Duplicate inbound `provider_guid` (Defect B) | rejected, `23505` |
| Suppression deletion writes an audit row before deleting | pass |
| **The running Node worker claimed a message scheduled for 2030** | **confirmed — §4 is required** |

All test rows were deleted afterwards and the deletion was asserted. The
`sender01` row is real configuration and was left in place.
