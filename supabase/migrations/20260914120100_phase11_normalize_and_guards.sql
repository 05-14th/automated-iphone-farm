-- Phase 11 [guide steps 31, 32, 33] - address normalization and integrity guards.

begin;

-- ---------------------------------------------------------------------------
-- normalize_address [guide step 31]
-- ---------------------------------------------------------------------------
create or replace function public.normalize_address(p_address text)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public
as $fn$
declare
  v_trimmed text;
  v_digits  text;
begin
  if p_address is null then
    return null;
  end if;

  v_trimmed := btrim(p_address);

  if v_trimmed = '' then
    return null;
  end if;

  -- Email identity: lowercase + trim. This is the primary address shape in this
  -- architecture - the sender identity is an Apple Account email, not a phone number.
  if position('@' in v_trimmed) > 0 then
    return lower(v_trimmed);
  end if;

  -- Otherwise treat it as a phone number: strip everything that is not a digit,
  -- then render E.164.
  v_digits := regexp_replace(v_trimmed, '[^0-9]', '', 'g');

  if v_digits = '' then
    -- Neither an email nor anything phone-shaped. Fall back to a lowercased token so
    -- the value is at least deterministic and still comparable.
    return lower(v_trimmed);
  end if;

  if left(v_trimmed, 1) = '+' then
    -- Already international; the digits are the whole number.
    return '+' || v_digits;
  end if;

  if length(v_digits) = 10 then
    -- NANP local form, e.g. 7783231234 -> +17783231234.
    return '+1' || v_digits;
  end if;

  if length(v_digits) = 11 and left(v_digits, 1) = '1' then
    return '+' || v_digits;
  end if;

  -- Best effort for any other length; deterministic and round-trippable.
  return '+' || v_digits;
end;
$fn$;

comment on function public.normalize_address(text) is
  'Guide step 31: "normalize recipient identifiers before suppression/routing checks". '
  'Emails are lowercased and trimmed; anything else is reduced to digits and rendered E.164 '
  '(bare 10-digit input is assumed NANP and gets +1). Deterministic and IMMUTABLE so it can '
  'back a unique index if that is ever wanted. Applied automatically by BEFORE INSERT OR UPDATE '
  'triggers on contacts and suppressions - a suppression list compared against un-normalized '
  'input is a suppression list that silently leaks.';

-- ---------------------------------------------------------------------------
-- Normalization triggers on contacts and suppressions
-- ---------------------------------------------------------------------------
create or replace function public.tg_normalize_contact_address()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
begin
  if new.raw_address is null or btrim(new.raw_address) = '' then
    -- Allow callers to supply only normalized_address; keep raw as the given value.
    new.raw_address := new.normalized_address;
  end if;

  new.normalized_address := public.normalize_address(
    coalesce(new.normalized_address, new.raw_address)
  );

  if new.normalized_address is null then
    raise exception 'contacts.normalized_address cannot be empty (raw_address = %)', new.raw_address
      using errcode = '23514';
  end if;

  return new;
end;
$fn$;

create trigger trg_contacts_normalize_address
  before insert or update of normalized_address, raw_address on public.contacts
  for each row execute function public.tg_normalize_contact_address();

comment on function public.tg_normalize_contact_address() is
  'Guide step 31. Forces contacts.normalized_address through normalize_address() on every write '
  'so contacts_normalized_address_uidx really is one row per real-world address.';

create or replace function public.tg_normalize_suppression_address()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
begin
  if new.raw_address is null or btrim(new.raw_address) = '' then
    new.raw_address := new.normalized_address;
  end if;

  new.normalized_address := public.normalize_address(
    coalesce(new.normalized_address, new.raw_address)
  );

  if new.normalized_address is null then
    raise exception 'suppressions.normalized_address cannot be empty (raw_address = %)', new.raw_address
      using errcode = '23514';
  end if;

  return new;
end;
$fn$;

create trigger trg_suppressions_normalize_address
  before insert or update of normalized_address, raw_address on public.suppressions
  for each row execute function public.tg_normalize_suppression_address();

comment on function public.tg_normalize_suppression_address() is
  'Guide step 36. Same normalization as contacts, so a STOP recorded as "(778) 323-1234" '
  'suppresses a contact stored as "+17783231234".';

-- ---------------------------------------------------------------------------
-- Sticky routing guard [guide step 32]
-- ---------------------------------------------------------------------------
create or replace function public.tg_contacts_sticky_sender_immutable()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
begin
  if new.sticky_sender_id is distinct from old.sticky_sender_id then
    raise exception
      'contacts.sticky_sender_id is immutable (guide step 32: never silently reroute a contact '
      'to another sender; pause the failing sender and alert instead). contact_id=%, % -> %',
      old.id, old.sticky_sender_id, new.sticky_sender_id
      using errcode = '23514';
  end if;
  return new;
end;
$fn$;

create trigger trg_contacts_sticky_sender_immutable
  before update on public.contacts
  for each row execute function public.tg_contacts_sticky_sender_immutable();

comment on function public.tg_contacts_sticky_sender_immutable() is
  'Guide step 32. Hard-blocks any change to a contact''s sticky sender. Rerouting is the '
  'tempting "fix" when a sender fails and it is exactly the wrong one: the recipient would see '
  'the conversation continue from a stranger''s address.';

-- ---------------------------------------------------------------------------
-- Sticky routing guard on conversations and messages [guide step 32]
-- ---------------------------------------------------------------------------
create or replace function public.tg_conversation_sender_must_be_sticky()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
declare
  v_sticky uuid;
begin
  select c.sticky_sender_id into v_sticky
  from public.contacts c
  where c.id = new.contact_id;

  if v_sticky is null then
    raise exception 'contact % not found', new.contact_id using errcode = '23503';
  end if;

  if new.sender_id <> v_sticky then
    raise exception
      'conversation sender % does not match contact %''s sticky sender % (guide step 32)',
      new.sender_id, new.contact_id, v_sticky
      using errcode = '23514';
  end if;

  return new;
end;
$fn$;

create trigger trg_conversations_sender_must_be_sticky
  before insert or update of contact_id, sender_id on public.conversations
  for each row execute function public.tg_conversation_sender_must_be_sticky();

create or replace function public.tg_message_sender_must_match_conversation()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
declare
  v_conv_sender uuid;
begin
  select cv.sender_id into v_conv_sender
  from public.conversations cv
  where cv.id = new.conversation_id;

  if v_conv_sender is null then
    raise exception 'conversation % not found', new.conversation_id using errcode = '23503';
  end if;

  if new.sender_id <> v_conv_sender then
    raise exception
      'message sender % does not match conversation %''s sender % (guide step 32)',
      new.sender_id, new.conversation_id, v_conv_sender
      using errcode = '23514';
  end if;

  return new;
end;
$fn$;

create trigger trg_messages_sender_must_match_conversation
  before insert or update of conversation_id, sender_id on public.messages
  for each row execute function public.tg_message_sender_must_match_conversation();

-- ---------------------------------------------------------------------------
-- Send state machine [guide step 33] - Defect A lives in this transition table
-- ---------------------------------------------------------------------------
create or replace function public.tg_messages_state_machine()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
begin
  if new.state = old.state then
    return new;
  end if;

  -- queued -> sending -> sent | failed, plus two recovery edges.
  if old.state = 'queued' and new.state in ('sending', 'failed') then
    null;

  elsif old.state = 'sending' and new.state in ('sent', 'failed') then
    null;

  elsif old.state = 'failed' and new.state = 'queued' then
    -- Defect A: a failed message may only go back into the queue once it has been
    -- reconciled against provider state and proven NOT to have been delivered.
    if not new.reconciled then
      raise exception
        'message % cannot be requeued before reconciliation (Defect A: BlueBubbles returns 500 '
        'or times out on sends that actually delivered; retrying blind double-sends). Set '
        'reconciled = true after checking provider state by temp_guid.', old.id
        using errcode = '23514';
    end if;

  elsif old.state = 'failed' and new.state = 'sent' then
    -- Defect A recovery: reconciliation discovered the "failed" send had in fact delivered.
    if not new.reconciled then
      raise exception
        'message % cannot move failed -> sent without reconciled = true (Defect A recovery path)',
        old.id
        using errcode = '23514';
    end if;

  else
    raise exception
      'illegal message state transition % -> % on message % (guide step 33: queued -> sending -> '
      'sent | failed; sent is terminal)', old.state, new.state, old.id
      using errcode = '23514';
  end if;

  -- Stamp the state machine timestamps if the caller did not.
  if new.state = 'sending' and new.sending_at is null then
    new.sending_at := now();
  elsif new.state = 'sent' and new.sent_at is null then
    new.sent_at := now();
  elsif new.state = 'failed' and new.failed_at is null then
    new.failed_at := now();
  elsif new.state = 'queued' then
    new.queued_at := now();
  end if;

  return new;
end;
$fn$;

create trigger trg_messages_state_machine
  before update of state on public.messages
  for each row execute function public.tg_messages_state_machine();

comment on function public.tg_messages_state_machine() is
  'Guide step 33 + Defect A. Enforces queued -> sending -> sent | failed, treats sent as '
  'terminal, and permits the two recovery edges failed -> queued and failed -> sent ONLY when '
  'reconciled = true. That single condition is what stops the measured Defect A behaviour '
  '(HTTP 500 / 30s timeout on a message that actually delivered) from turning into a duplicate '
  'send. Also auto-stamps sending_at / sent_at / failed_at.';

commit;
