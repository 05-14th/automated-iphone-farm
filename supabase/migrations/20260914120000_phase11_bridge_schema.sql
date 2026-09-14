-- Phase 11 [guide step 31] - iMessage sending bridge data model.
--
-- Architecture: Portal / Supabase worker -> BlueBubbles -> Messages.app -> recipient.
-- This migration creates ONLY the data model. No worker/sending code.
--
-- Two measured defects from docs/PHASE1-REPORT.md are designed around here:
--   Defect A - the BlueBubbles API returns HTTP 500 / times out at 30s on sends that
--              ACTUALLY DELIVERED. The response is not truth. Reconcile before retry.
--              Absorbed by: messages.temp_guid (+ unique index), messages.reconciled,
--              and the send_attempts audit table.
--   Defect B - BlueBubbles dispatches duplicate `new-message` webhooks carrying an
--              IDENTICAL provider GUID (measured 8 of 11 outbound sends).
--              Absorbed by: messages_provider_guid_uidx.

begin;

-- ---------------------------------------------------------------------------
-- senders [guide steps 31, 32]
-- ---------------------------------------------------------------------------
create table if not exists public.senders (
  id               uuid primary key default gen_random_uuid(),
  slug             text not null unique,
  apple_email      text not null unique,
  bluebubbles_url  text not null,
  bluebubbles_port integer not null,
  macos_user       text not null,
  status           text not null default 'active',
  paused_reason    text,
  created_at       timestamptz not null default now(),
  constraint senders_slug_format_chk
    check (slug ~ '^sender0[1-5]$'),
  constraint senders_status_chk
    check (status in ('active', 'paused', 'failed')),
  constraint senders_port_chk
    check (bluebubbles_port between 1 and 65535),
  -- A non-active sender must say why. Operators need the reason in the alert,
  -- because the system will NOT reroute around the failure on its own.
  constraint senders_paused_reason_chk
    check (status = 'active' or paused_reason is not null)
);

comment on table public.senders is
  'Guide step 31/32. One row per iMessage sender identity (sender01..sender05), each an '
  'iMessage-enabled Apple Account email driven by its own BlueBubbles instance on its own '
  'macOS user and port. Routing is STICKY: when a sender fails it is set to status=paused '
  '(or failed) and its traffic simply STOPS. Nothing is ever auto-rerouted to another '
  'sender - a rerouted contact would see a message arrive from a different identity, which '
  'is the single worst trust failure in this channel. Pause and alert instead.';
comment on column public.senders.slug is 'Stable operator handle, sender01..sender05 (guide step 28 adds sender02 on port 12342).';
comment on column public.senders.apple_email is 'The iMessage email identity messages are sent FROM. Not a phone number - this is the email-based architecture.';
comment on column public.senders.bluebubbles_url is 'Base URL of this sender''s BlueBubbles server, e.g. http://127.0.0.1 (Phase 1 binds localhost only).';
comment on column public.senders.bluebubbles_port is 'Per-sender BlueBubbles port. sender01=12341, sender02=12342, ... (guide steps 11, 28).';
comment on column public.senders.macos_user is 'macOS Standard user whose console session runs Messages + BlueBubbles, e.g. imsg01.';
comment on column public.senders.status is 'active | paused | failed. Only active senders may send. paused/failed halt traffic; they never redirect it.';
comment on column public.senders.paused_reason is 'Human-readable cause recorded when status leaves active. Required by senders_paused_reason_chk.';

-- ---------------------------------------------------------------------------
-- contacts [guide steps 31, 32]
-- ---------------------------------------------------------------------------
create table if not exists public.contacts (
  id                 uuid primary key default gen_random_uuid(),
  normalized_address text not null,
  raw_address        text not null,
  display_name       text,
  sticky_sender_id   uuid not null references public.senders(id) on delete restrict,
  created_at         timestamptz not null default now()
);

create unique index if not exists contacts_normalized_address_uidx
  on public.contacts (normalized_address);

comment on table public.contacts is
  'Guide step 31/32. One row per recipient identity. normalized_address is the canonical key '
  'for every suppression and routing check; raw_address keeps whatever the operator typed. '
  'sticky_sender_id is assigned once, on first contact, and is NEVER changed - enforced by '
  'trg_contacts_sticky_sender_immutable.';
comment on column public.contacts.normalized_address is
  'Canonical address: lowercased/trimmed email, or E.164 phone (+1XXXXXXXXXX). Maintained by '
  'trg_contacts_normalize_address; do not write it by hand.';
comment on column public.contacts.raw_address is 'Address exactly as supplied by the import/portal, kept for audit and display.';
comment on column public.contacts.sticky_sender_id is
  'Guide step 32. The ONE sender identity this contact will ever hear from. Immutable after '
  'insert. If that sender is paused/failed, this contact simply does not receive messages.';
comment on index public.contacts_normalized_address_uidx is
  'Guide step 31 ("normalize recipient identifiers before suppression/routing checks"). '
  'Guarantees one contact row per real-world address so a sticky sender cannot be '
  'accidentally forked by a differently-formatted duplicate of the same address.';

-- ---------------------------------------------------------------------------
-- conversations [guide steps 31, 34, 35]
-- ---------------------------------------------------------------------------
create table if not exists public.conversations (
  id                 uuid primary key default gen_random_uuid(),
  contact_id         uuid not null references public.contacts(id) on delete cascade,
  sender_id          uuid not null references public.senders(id) on delete restrict,
  provider_chat_guid text,
  created_at         timestamptz not null default now(),
  constraint conversations_contact_sender_uniq unique (contact_id, sender_id)
);

create unique index if not exists conversations_provider_chat_guid_uidx
  on public.conversations (provider_chat_guid)
  where provider_chat_guid is not null;

create index if not exists conversations_contact_idx
  on public.conversations (contact_id);

comment on table public.conversations is
  'Guide step 31/34. The (contact, sender) thread. FIFO is scoped HERE: messages inside one '
  'conversation must be sent strictly in order, while unrelated conversations progress '
  'independently. provider_chat_guid is null until BlueBubbles reports the chat GUID - note '
  'that POST /api/v1/chat/new can create the chat and STILL throw -1728 "Can''t get chat id" '
  '(Defect A), so a null guid never means "no chat exists".';
comment on column public.conversations.provider_chat_guid is
  'BlueBubbles chat GUID. Nullable because of Defect A (chat created, id not returned); '
  'unique when present so a reconciliation pass cannot attach one provider chat to two rows.';
comment on constraint conversations_contact_sender_uniq on public.conversations is
  'Guide step 32. One thread per contact per sender. Combined with the immutable '
  'contacts.sticky_sender_id this makes a second, wrong-sender thread unrepresentable.';

-- ---------------------------------------------------------------------------
-- messages [guide steps 31, 33, 34, 35] - Defects A and B both land here
-- ---------------------------------------------------------------------------
create table if not exists public.messages (
  id              uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  sender_id       uuid not null references public.senders(id) on delete restrict,
  direction       text not null,
  body            text,
  state           text not null default 'queued',
  provider_guid   text,
  temp_guid       text,
  error_code      text,
  queued_at       timestamptz not null default now(),
  sending_at      timestamptz,
  sent_at         timestamptz,
  delivered_at    timestamptz,
  failed_at       timestamptz,
  reconciled      boolean not null default false,
  created_at      timestamptz not null default now(),
  constraint messages_direction_chk
    check (direction in ('outbound', 'inbound')),
  constraint messages_state_chk
    check (state in ('queued', 'sending', 'sent', 'failed')),
  -- Inbound messages are observed facts: they arrive already carrying a provider GUID
  -- and are never queued/sent by us.
  constraint messages_inbound_shape_chk
    check (
      direction <> 'inbound'
      or (state = 'sent' and provider_guid is not null and temp_guid is null)
    ),
  -- Defect A: temp_guid is the reconciliation key. Every outbound message must carry one
  -- BEFORE the HTTP call, or a timeout leaves nothing to reconcile against.
  constraint messages_outbound_temp_guid_chk
    check (direction <> 'outbound' or temp_guid is not null),
  constraint messages_failed_shape_chk
    check (state <> 'failed' or failed_at is not null)
);

-- VERBATIM from the deployment guide, step 31. This is the Defect B kill switch.
create unique index messages_provider_guid_uidx on public.messages(provider_guid) where provider_guid is not null;

-- Defect A: the reconciliation key. One outbound message per temp_guid, so a webhook or a
-- provider-state poll arriving after an HTTP 500/timeout resolves to exactly one row.
create unique index messages_temp_guid_uidx
  on public.messages (temp_guid)
  where temp_guid is not null;

-- Guide step 34: per-conversation FIFO ordering scan.
create index if not exists messages_conversation_created_idx
  on public.messages (conversation_id, created_at);

-- Guide step 34: at most ONE message per conversation may be in flight at a time.
-- Without this, two workers can both claim the head of a queue and deliver out of order.
create unique index messages_one_sending_per_conversation_uidx
  on public.messages (conversation_id)
  where state = 'sending';

-- Worker claim path: oldest queued outbound message per sender.
create index if not exists messages_queued_outbound_idx
  on public.messages (sender_id, queued_at)
  where state = 'queued' and direction = 'outbound';

-- Defect A triage: everything that failed and has not yet been reconciled against
-- provider state. This set must be empty before any retry sweep is considered safe.
create index if not exists messages_unreconciled_failed_idx
  on public.messages (failed_at)
  where state = 'failed' and reconciled = false;

comment on table public.messages is
  'Guide steps 31/33/34/35. One row per message in either direction, and the state machine '
  'that governs sending: queued -> sending -> sent | failed. The BlueBubbles HTTP response is '
  'NOT authoritative (Defect A): a 500 or a 30s timeout routinely accompanies a message that '
  'was in fact delivered, and a send issued while the Mac is offline still returns HTTP 200 '
  'and is delivered minutes later. Truth comes from provider state / the echo webhook, matched '
  'on temp_guid or provider_guid.';
comment on column public.messages.direction is 'outbound (we sent it) | inbound (a reply, observed via webhook).';
comment on column public.messages.state is
  'queued -> sending -> sent | failed. Transitions are enforced by trg_messages_state_machine. '
  'failed -> sent is legal ONLY with reconciled = true: that is the Defect A recovery path.';
comment on column public.messages.provider_guid is
  'BlueBubbles message GUID. Unique when present (messages_provider_guid_uidx) - this is what '
  'makes the duplicate new-message webhooks of Defect B a no-op instead of a double-insert.';
comment on column public.messages.temp_guid is
  'Defect A reconciliation key. Client-generated, written BEFORE the HTTP request, echoed back '
  'by BlueBubbles as tempGuid. On a timeout or 5xx, look the message up by this value in '
  'provider state: if it is there, the send succeeded and must be marked sent+reconciled, not '
  'retried. Retrying blind on a timeout is how this stack double-sends.';
comment on column public.messages.error_code is 'Provider/transport error code for a failed send (e.g. HTTP status, AppleScript -1728).';
comment on column public.messages.sent_at is 'When the provider ACCEPTED the message. Acceptance is not delivery - see delivered_at.';
comment on column public.messages.delivered_at is
  'When the provider confirmed actual delivery (error=0 echo). A message accepted while the Mac '
  'was offline is delivered only after the link returns, so this can lag sent_at by minutes.';
comment on column public.messages.reconciled is
  'Defect A. True once this row has been checked against provider state after an ambiguous '
  'outcome (timeout / 5xx). A failed message with reconciled = false MUST NOT be retried.';

comment on index public.messages_provider_guid_uidx is
  'DEFECT B (guide steps 31, 35). BlueBubbles dispatches the same new-message webhook twice, '
  'about 1 ms apart, with an IDENTICAL GUID - measured on 8 of 11 outbound sends, and on 7 of 7 '
  'successful POST /api/v1/message/text calls, i.e. exactly the bridge hot path. Inbound events '
  'are at-least-once for the same reason. This index is the entire defence: every ingest writes '
  'with ON CONFLICT (provider_guid) DO NOTHING/UPDATE, and the second copy collapses into the '
  'first. Index definition is verbatim from the guide. Do not drop it.';
comment on index public.messages_temp_guid_uidx is
  'DEFECT A (guide step 33). The BlueBubbles API returns HTTP 500 or times out at 30s on sends '
  'that ACTUALLY DELIVERED (the await/reconcile step gives up before AppleScript settles). '
  'temp_guid is generated by the worker before the call, so after an ambiguous response the '
  'message can be located in provider state and reconciled instead of retried. Uniqueness '
  'guarantees that lookup resolves to exactly one row - without it, reconciliation itself could '
  'mark the wrong message sent and silently double-send the real one.';
comment on index public.messages_one_sending_per_conversation_uidx is
  'Guide step 34 (per-conversation FIFO). At most one in-flight send per conversation, enforced '
  'by the database rather than by worker etiquette.';

-- ---------------------------------------------------------------------------
-- consents [guide steps 31, 36]
-- ---------------------------------------------------------------------------
create table if not exists public.consents (
  id         uuid primary key default gen_random_uuid(),
  contact_id uuid not null references public.contacts(id) on delete cascade,
  status     text not null,
  source     text not null,
  evidence   jsonb not null default '{}'::jsonb,
  granted_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  constraint consents_status_chk
    check (status in ('granted', 'revoked')),
  constraint consents_timestamps_chk
    check (
      (status = 'granted' and granted_at is not null)
      or (status = 'revoked' and revoked_at is not null)
    )
);

create index if not exists consents_contact_created_idx
  on public.consents (contact_id, created_at desc);

comment on table public.consents is
  'Guide step 31/36. Append-only consent ledger - rows are never mutated, a revocation is a NEW '
  'row with status = revoked. The current verdict is the most recent row per contact, which is '
  'what can_send() evaluates. evidence holds the proof (form payload, IP, timestamp, recording '
  'reference). Qualified counsel must validate the exact consent, identification and unsubscribe '
  'requirements for every jurisdiction contacted; this table only records what happened.';
comment on column public.consents.source is 'Where consent came from: web_form, import, verbal, reply_optin, operator, ...';
comment on column public.consents.evidence is 'Immutable proof blob for the audit trail.';

-- ---------------------------------------------------------------------------
-- suppressions [guide steps 31, 36]
-- ---------------------------------------------------------------------------
create table if not exists public.suppressions (
  id                 uuid primary key default gen_random_uuid(),
  normalized_address text not null,
  raw_address        text,
  reason             text not null,
  created_at         timestamptz not null default now(),
  constraint suppressions_reason_chk
    check (reason in ('stop', 'dnc', 'bounce', 'manual'))
);

create unique index if not exists suppressions_normalized_address_uidx
  on public.suppressions (normalized_address);

comment on table public.suppressions is
  'Guide step 36. STOP/DNC suppression, keyed on the normalized address and GLOBAL across every '
  'sender and every conversation. There is deliberately no sender_id column: a person who says '
  'STOP to sender01 has said STOP to the operator, and must never be reachable from sender03. '
  'Suppression is the FIRST check in can_send() and outranks consent.';
comment on column public.suppressions.normalized_address is
  'Canonical address, maintained by trg_suppressions_normalize_address. Matching a suppression '
  'against a raw, unnormalized string is how suppression lists get silently bypassed.';
comment on column public.suppressions.reason is 'stop (recipient opt-out) | dnc (registry/legal) | bounce (undeliverable) | manual (operator).';
comment on index public.suppressions_normalized_address_uidx is
  'Guide step 31 ("normalize recipient identifiers before suppression/routing checks"). One row '
  'per real address; makes the suppression lookup in can_send() an exact-match index probe.';

-- ---------------------------------------------------------------------------
-- send_attempts [guide step 33] - the Defect A audit trail
-- ---------------------------------------------------------------------------
create table if not exists public.send_attempts (
  id          uuid primary key default gen_random_uuid(),
  message_id  uuid not null references public.messages(id) on delete cascade,
  attempt_no  integer not null,
  http_status integer,
  response    jsonb,
  outcome     text not null,
  created_at  timestamptz not null default now(),
  constraint send_attempts_outcome_chk
    check (outcome in ('accepted', 'timeout', 'error', 'reconciled_sent')),
  constraint send_attempts_attempt_no_chk
    check (attempt_no >= 1),
  constraint send_attempts_message_attempt_uniq unique (message_id, attempt_no)
);

create index if not exists send_attempts_message_idx
  on public.send_attempts (message_id, attempt_no);

comment on table public.send_attempts is
  'DEFECT A audit trail (guide step 33). One row per HTTP call to BlueBubbles, written whatever '
  'the outcome. This is what makes Defect A debuggable after the fact: a message whose history '
  'reads timeout -> reconciled_sent is a correctly handled false failure, whereas timeout -> '
  'accepted is a double-send and a pilot-gate violation (guide step 38 tracks "unreconciled '
  'timeouts" and "duplicate sends" explicitly). http_status 500 here does NOT imply the message '
  'failed - the measured defect is precisely that it often delivered.';
comment on column public.send_attempts.outcome is
  'accepted (2xx from provider - still not proof of delivery) | timeout (no response in 30s, '
  'message status UNKNOWN) | error (explicit provider failure) | reconciled_sent (a later '
  'provider-state lookup by temp_guid proved this ambiguous attempt actually delivered).';
comment on column public.send_attempts.response is 'Raw provider response/exception payload, kept verbatim for forensics.';
comment on column public.send_attempts.attempt_no is
  'Monotonic per message, starting at 1. attempt_no > 1 is only ever legal after the previous '
  'attempt was reconciled against provider state - never retry blind on a timeout.';

commit;
