# iMessage sending system

Sends and receives iMessage at low volume through a Mac mini, with consent, suppression
and delivery tracking. Built against `Email_Based_BlueBubbles_iMessage_Deployment_Guide.pdf`.

**You never touch the Mac.** You call an API; the Mac does the sending on its own and
recovers by itself after a crash or power cut.

---

## 1. How it fits together

```
   your system
        |
        |  HTTPS + bearer token
        v
   +----------------------+
   |  Python REST API     |   api/      <- you deploy this on your own server
   |  (FastAPI)           |
   +----------+-----------+
              |
              |  writes the work down
              v
   +----------------------+
   |  Supabase (Postgres) |   the meeting point; enforces every safety rule
   +----------+-----------+
              |
              |  Mac polls for work
              v
   +--------------------------------------------------+
   |  Mac mini (must stay powered on and online)       |
   |                                                   |
   |   worker  ->  BlueBubbles  ->  Messages.app  ->  recipient
   |   listener <-  BlueBubbles  <-  Messages.app  <-  reply
   +--------------------------------------------------+
```

**Why two halves.** BlueBubbles only accepts connections from the Mac itself. Exposing it
to the internet would let anyone who finds it send as you. So the API never talks to it.
The API records work in the database, the Mac picks it up. If the Mac goes offline,
requests keep being accepted and go out when it returns.

## 2. Quick start

```bash
# your server
export API=https://your-server.example.com
export TOKEN=<your API_TOKEN>

# send now
curl -X POST "$API/v1/messages" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to":"+17783231234","body":"Hello from Clario"}'

# send later
curl -X POST "$API/v1/messages" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to":"+17783231234","body":"Morning","mode":"scheduled","send_at":"2026-09-16T14:00:00Z"}'

# read replies
curl "$API/v1/inbox?since=2026-09-15T00:00:00Z" -H "Authorization: Bearer $TOKEN"
```

Interactive docs for every endpoint: **`$API/docs`**.

## 3. The first message to someone takes ~2 minutes

This is the single most surprising behaviour and it is not a bug.

| | Time |
|---|---|
| First ever message to a person | **75-120 s** |
| Every message after that | **< 1 s** |

Opening a new conversation is slow and *always reports failure even when it worked*. The
system sends anyway, checks afterwards what really happened, and corrects the record. If
it simply retried on failure, every new contact would be messaged twice.

So reaching 100 new people takes hours, not minutes. Plan around it. It is also closer to
a safe sending rate than a burst would be.

## 4. Who you can actually reach

iMessage only reaches addresses **registered with Apple**. An ordinary email address whose
owner never linked it to an Apple account is unreachable — like texting a landline.

- **Phone numbers of iPhone users** — reliable. iPhones register automatically.
- **Email addresses** — usually fail. Registration is opt-in and rare.

An unreachable address costs ~90 s before failing, then gets suppressed so it is refused
instantly forever after. Check your list before loading it: on your iPhone, blue means
reachable, green means not.

## 5. Rules the system enforces for you

These are enforced by the **database**, not by code politeness, so nothing can bypass them —
not a bug, not a future change, not a direct query.

| Rule | What it means |
|---|---|
| Consent required | No consent record, no send. Refused at the door. |
| Suppression is global and permanent | One opt-out blocks every sender forever. |
| Sticky sender | A contact keeps its first sender for life. A failing sender pauses; it never silently reroutes. |
| One message at a time per conversation | Messages in a thread cannot overtake each other. |
| No duplicate delivery | Rejected by a unique constraint on the provider's own message id. |
| No blind retry | A timed-out send physically cannot be retried until the provider has been checked. |
| Sending caps | Per-sender hourly, daily and new-conversation limits, plus quiet hours. |

**Opt-outs are automatic.** A reply of *stop*, *unsubscribe*, *cancel* and similar suppresses
that person across every sender immediately, without you doing anything.

## 6. Everyday tasks

| Task | Call |
|---|---|
| Send now | `POST /v1/messages` |
| Send later | `POST /v1/messages` with `mode:"scheduled"` |
| Send to many | `POST /v1/messages/bulk` (max 100; one refusal does not sink the rest) |
| Cancel a scheduled message | `DELETE /v1/messages/{id}` (before pickup) |
| Did it arrive? | `GET /v1/messages/{id}` — look at `state` and `provider_guid` |
| Read replies | `GET /v1/inbox` |
| A whole thread | `GET /v1/conversations/{id}/messages` |
| May I contact this person? | `GET /v1/contacts/{address}` |
| Record consent | `POST /v1/consents` |
| Block someone | `POST /v1/suppressions` |
| Is everything healthy? | `GET /v1/health` (no auth) |
| Am I near a cap? | `GET /v1/senders` |

### Message states

`queued` → `sending` → `sent`, or `failed`.

**`failed` does not always mean undelivered.** Check `reconciled`. A `failed` message with
`reconciled: true` and a `provider_guid` **did** arrive; the interface merely lied about it.
A `failed` message with `reconciled: false` is genuinely unknown and needs a human.

## 7. Stopping and starting

**To stop sending**, pause the sender. Do not kill processes — they restart themselves.

```sql
update senders set status='paused', paused_reason='operator' where slug='sender01';
```

Queued messages wait; nothing is lost. Set `status='active'` to resume.

## 8. What must keep running on the Mac

All of it starts automatically at boot with nobody logged in, and restarts if it crashes.
See `ops/README.md`.

| Service | Purpose |
|---|---|
| `com.clario.bridge-worker` | Sends queued messages |
| `com.clario.bridge-health` | Marks a sender failed when its software dies |
| `com.clario.bb-webhook-listener` | Records incoming messages |
| `com.imsg.launch-bluebubbles` | Starts BlueBubbles (needs the desktop session; auto-login covers it) |

The Mac must stay **powered on and online**. That is the one hard dependency.

## 9. Where everything lives

| Path | What |
|---|---|
| `api/` | Python REST API — deploy on your server. See `api/README.md`, `api/DEPLOYMENT.md` |
| `bridge/` | Mac-side worker, health checker, internal gateway |
| `ops/` | Service definitions for the Mac |
| `supabase/migrations/` | Database schema |
| `scripts/` | Phase 1 acceptance tests |
| `docs/PHASE1-REPORT.md` | Every measured result and the traps found along the way |
| `docs/BRIDGE-SCHEMA.md` | Data model and why each rule exists |
| `docs/PILOT-CRITERIA.md` | Pass/fail definition — agree before real sending |

## 10. Before real outreach

1. **Counsel sign-off** on consent, sender identification and opt-out handling.
   Canadian anti-spam rules apply; iMessage is not exempt because it is not email.
2. **Fill in the business targets** in `docs/PILOT-CRITERIA.md`.
3. **A recipient list of reachable addresses** — see section 4.
4. **A second Apple ID** if you want a second sender. Verify its email and attach a phone
   number *before* signing in on the Mac.

## 11. Known limitations

- **One sender today.** Adding more needs separate Apple accounts and the guide's 48-hour
  two-sender test.
- **Confirming a timed-out send** matches on message text within a time window, because the
  provider does not store our correlation token. Two identical messages to the same person
  in the same short window would be indistinguishable.
- **Scheduling far ahead blocks that conversation** until the message goes out. Deliberate:
  the alternative lets a later message overtake an earlier one in a thread someone reads.
- **Incoming messages are captured only by the Mac-side listener.** If it stops, replies and
  opt-outs stop being recorded even though sending still works.
