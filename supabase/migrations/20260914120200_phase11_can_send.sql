-- Phase 11 [guide step 36] - the pre-send gate.

begin;

create or replace function public.can_send(p_contact_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $fn$
declare
  v_contact      public.contacts%rowtype;
  v_sender       public.senders%rowtype;
  v_consent      public.consents%rowtype;
  v_conv         public.conversations%rowtype;
  v_inflight     uuid;
begin
  -- 0. The contact must exist at all.
  select * into v_contact from public.contacts where id = p_contact_id;
  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'contact_not_found');
  end if;

  -- 1. SUPPRESSION first, and globally. Guide step 36: "STOP/DNC must suppress globally".
  --    This outranks everything, including a freshly granted consent - if someone has said
  --    STOP, a later opt-in record does not reopen the channel without an operator clearing
  --    the suppression deliberately.
  if exists (
    select 1
    from public.suppressions s
    where s.normalized_address = v_contact.normalized_address
  ) then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'suppressed:' || (
        select s.reason
        from public.suppressions s
        where s.normalized_address = v_contact.normalized_address
        limit 1
      )
    );
  end if;

  -- 2. VALID CONSENT. The consent ledger is append-only; the newest row is the verdict.
  select * into v_consent
  from public.consents c
  where c.contact_id = v_contact.id
  order by c.created_at desc, c.id desc
  limit 1;

  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'no_consent_on_record');
  end if;

  if v_consent.status <> 'granted' then
    return jsonb_build_object('allowed', false, 'reason', 'consent_revoked');
  end if;

  if v_consent.granted_at is null or v_consent.granted_at > now() then
    return jsonb_build_object('allowed', false, 'reason', 'consent_not_yet_effective');
  end if;

  -- 3. SENDER PAUSED. Guide step 32: a paused sender STOPS. It is never routed around.
  select * into v_sender from public.senders where id = v_contact.sticky_sender_id;
  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'sticky_sender_missing');
  end if;

  if v_sender.status <> 'active' then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'sender_' || v_sender.status || ':' || v_sender.slug
    );
  end if;

  -- 4. STICKY-SENDER MATCH. If a conversation already exists it must belong to the
  --    contact's first-and-only sender. A mismatch is a data-integrity alarm, not a
  --    thing to silently correct.
  select * into v_conv
  from public.conversations cv
  where cv.contact_id = v_contact.id
  order by cv.created_at asc
  limit 1;

  if found and v_conv.sender_id <> v_contact.sticky_sender_id then
    return jsonb_build_object('allowed', false, 'reason', 'sticky_sender_mismatch');
  end if;

  -- 5. IN-FLIGHT MESSAGE. Guide step 34 (per-conversation FIFO): one message at a time in
  --    a conversation. This is also the second line of defence against Defect A - while a
  --    send whose outcome is unknown is still queued or sending, nothing else may go out on
  --    that thread and compound the ambiguity.
  if found then
    select m.id into v_inflight
    from public.messages m
    where m.conversation_id = v_conv.id
      and m.direction = 'outbound'
      and m.state in ('queued', 'sending')
    limit 1;

    if v_inflight is not null then
      return jsonb_build_object('allowed', false, 'reason', 'in_flight_message_exists');
    end if;
  end if;

  return jsonb_build_object('allowed', true, 'reason', 'ok');
end;
$fn$;

comment on function public.can_send(uuid) is
  'Guide step 36. The single pre-send gate. Returns {"allowed": bool, "reason": text} and '
  'evaluates in this fixed order, returning on the first failure: '
  '(1) suppression - global across all senders, outranks consent; '
  '(2) valid consent - newest row in the append-only consents ledger must be granted; '
  '(3) sender paused - a non-active sticky sender halts traffic, it is never rerouted (step 32); '
  '(4) sticky-sender match - an existing conversation must belong to the contact''s first sender; '
  '(5) in-flight message - per-conversation FIFO (step 34), and a guard against piling new sends '
  'on top of a send whose outcome is still unknown (Defect A). '
  'Order matters: a suppressed contact must never be evaluated for consent, and a paused sender '
  'must never be evaluated for an alternative. SECURITY DEFINER so the worker gets a truthful '
  'verdict regardless of the caller''s RLS view - never widen this to accept a sender override.';

revoke all on function public.can_send(uuid) from public;
revoke all on function public.can_send(uuid) from anon, authenticated;

commit;
