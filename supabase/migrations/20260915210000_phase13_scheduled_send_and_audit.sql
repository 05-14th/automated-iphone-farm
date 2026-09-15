-- Phase 13 - scheduled sending, an inbound read flag, and a suppression-deletion audit.
--
-- Added for the Python REST API under api/. Three additive changes, all designed so
-- that the EXISTING Node worker (bridge/worker/worker.mjs) keeps behaving EXACTLY as
-- it does today until it is changed deliberately:
--
--   1. messages.scheduled_for  - NULL means "send as soon as the worker picks it up",
--                                which is today's behaviour for every existing row and
--                                every row the Node gateway writes. A non-NULL value
--                                means "not before this instant".
--   2. messages.read_at        - operator-facing read flag for the inbound inbox.
--   3. suppression_deletions   - append-only audit of every un-suppression.
--
-- No column is made NOT NULL, no default changes, no constraint tightens, and no
-- existing index is dropped. Backfill is therefore unnecessary: every pre-existing
-- message row gets scheduled_for = NULL and is treated as due immediately.
--
-- !! REQUIRED WORKER CHANGE (not made here - bridge/ is out of scope for this migration).
-- Until bridge/lib/db.mjs::claimableMessages() filters on scheduled_for, the Node worker
-- will claim a scheduled message immediately and send it EARLY. The exact change is
-- documented in api/README.md and docs/API-SCHEDULED-SENDING.md.

begin;

-- ---------------------------------------------------------------------------
-- 1. scheduled sending
-- ---------------------------------------------------------------------------
alter table public.messages
  add column if not exists scheduled_for timestamptz;

comment on column public.messages.scheduled_for is
  'Earliest instant at which an outbound message may be handed to BlueBubbles. NULL = due '
  'immediately, which is the behaviour of every row written before this column existed and of '
  'every "quick" send. A worker MUST treat a row with scheduled_for > now() as not claimable: '
  'the message stays in state=queued (it is genuinely queued, just not yet due) rather than '
  'gaining a state of its own, so the send state machine, the per-conversation FIFO index and '
  'the Defect A reconciliation rules all continue to apply unchanged.';

-- An inbound message is an observed fact; it is never scheduled by us.
alter table public.messages
  drop constraint if exists messages_scheduled_outbound_chk;
alter table public.messages
  add constraint messages_scheduled_outbound_chk
  check (scheduled_for is null or direction = 'outbound');

-- The claim path for a worker that respects scheduling: oldest DUE message per sender.
-- Partial, because the overwhelming majority of rows are unscheduled and the existing
-- messages_queued_outbound_idx already serves those.
create index if not exists messages_scheduled_due_idx
  on public.messages (sender_id, scheduled_for)
  where state = 'queued' and direction = 'outbound' and scheduled_for is not null;

comment on index public.messages_scheduled_due_idx is
  'Claim path for scheduled sends: the due-ness scan. Partial on scheduled_for is not null so '
  'it stays small regardless of how much unscheduled traffic passes through the table.';

-- ---------------------------------------------------------------------------
-- 2. inbound read flag
-- ---------------------------------------------------------------------------
alter table public.messages
  add column if not exists read_at timestamptz;

comment on column public.messages.read_at is
  'When an operator/consumer marked this INBOUND message as read. Purely operator-facing: it '
  'has no effect on sending, on can_send() or on any trigger. NULL = unread, which is the '
  'state of every row that existed before this column.';

alter table public.messages
  drop constraint if exists messages_read_inbound_chk;
alter table public.messages
  add constraint messages_read_inbound_chk
  check (read_at is null or direction = 'inbound');

create index if not exists messages_inbound_unread_idx
  on public.messages (created_at desc)
  where direction = 'inbound' and read_at is null;

-- ---------------------------------------------------------------------------
-- 3. suppression deletion audit [guide step 36]
-- ---------------------------------------------------------------------------
create table if not exists public.suppression_deletions (
  id                 uuid primary key default gen_random_uuid(),
  normalized_address text not null,
  raw_address        text,
  reason             text not null,
  suppressed_at      timestamptz,
  deleted_by         text not null,
  source_ip          text,
  note               text,
  deleted_at         timestamptz not null default now()
);

create index if not exists suppression_deletions_address_idx
  on public.suppression_deletions (normalized_address, deleted_at desc);

comment on table public.suppression_deletions is
  'Guide step 36. Append-only audit of every removal from the suppression list. Deleting a '
  'suppression makes a person who opted out reachable again - it is the single most '
  'consequential write the API offers, and it must never happen without a record of WHO did it '
  'and WHEN. The row is written BEFORE the delete, so a crash between the two leaves an '
  'over-recorded audit trail rather than a silent un-suppression. Nothing ever updates or '
  'deletes a row here.';
comment on column public.suppression_deletions.deleted_by is
  'The caller''s claimed identity (X-Actor header) or "api_token" when none was supplied. A '
  'CLAIM, not proof: the API authenticates one shared bearer token, so source_ip is recorded '
  'alongside it and neither should be read as an authenticated identity.';

alter table public.suppression_deletions enable row level security;

drop policy if exists suppression_deletions_deny_all on public.suppression_deletions;
create policy suppression_deletions_deny_all
  on public.suppression_deletions
  for all
  to public
  using (false)
  with check (false);

revoke all on table public.suppression_deletions from anon, authenticated;

commit;
