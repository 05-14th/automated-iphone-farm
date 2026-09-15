-- Phase 12 [guide steps 33, 34, 36] - the worker-side pre-send gate.
--
-- WHY THIS EXISTS
--
-- can_send(contact_id) is the pre-ENQUEUE gate. Its check 5 refuses whenever any
-- outbound message on the conversation is queued or sending - and once the worker
-- has a message to send, that message is itself queued or sending. So can_send()
-- alone can never return allowed = true for a real send. A worker that "honoured"
-- it literally would never send anything; a worker that ignored the
-- in_flight_message_exists reason would be bypassing the gate.
--
-- can_send_message(message_id) resolves that without weakening anything:
--
--   * It CALLS can_send() for checks 0-4 (contact exists, suppression, consent,
--     sender status, sticky-sender match). None of that logic is duplicated here,
--     so it cannot drift. can_send() remains the single authority on whether this
--     contact may be messaged at all.
--   * It re-evaluates ONLY check 5, with the target message excluded, and demands
--     that the target be the FIFO head of its conversation: no other message may
--     be 'sending', and no other message may be 'queued' ahead of it.
--
-- Anything can_send() refuses for any other reason is passed straight through,
-- unmodified. There is deliberately no sender override parameter here either -
-- that would be the exact hole through which sticky routing gets bypassed.

begin;

create or replace function public.can_send_message(p_message_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $fn$
declare
  v_msg     public.messages%rowtype;
  v_conv    public.conversations%rowtype;
  v_verdict jsonb;
begin
  select * into v_msg from public.messages where id = p_message_id;
  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'message_not_found');
  end if;

  if v_msg.direction <> 'outbound' then
    return jsonb_build_object('allowed', false, 'reason', 'not_outbound');
  end if;

  if v_msg.state not in ('queued', 'sending') then
    -- 'sent' is terminal and 'failed' must go through reconciliation first
    -- (Defect A). Neither is sendable from here.
    return jsonb_build_object('allowed', false, 'reason', 'message_state:' || v_msg.state);
  end if;

  select * into v_conv from public.conversations where id = v_msg.conversation_id;
  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'conversation_not_found');
  end if;

  -- Checks 0-4 come from can_send(), verbatim and unmodified.
  v_verdict := public.can_send(v_conv.contact_id);

  if (v_verdict->>'allowed')::boolean then
    -- can_send() saw nothing in flight at all. Nothing left to re-check.
    return v_verdict;
  end if;

  if v_verdict->>'reason' <> 'in_flight_message_exists' then
    -- Suppressed, no consent, sender paused, sticky mismatch. Pass it straight
    -- through - this function never softens any of those.
    return v_verdict;
  end if;

  -- Check 5, re-evaluated with the target message excluded. The target must be
  -- the head of its conversation's FIFO queue.
  if exists (
    select 1
    from public.messages o
    where o.conversation_id = v_msg.conversation_id
      and o.direction = 'outbound'
      and o.id <> v_msg.id
      and (
        o.state = 'sending'
        or (o.state = 'queued' and (o.queued_at, o.id) < (v_msg.queued_at, v_msg.id))
      )
  ) then
    return jsonb_build_object('allowed', false, 'reason', 'in_flight_message_exists');
  end if;

  return jsonb_build_object('allowed', true, 'reason', 'ok');
end;
$fn$;

comment on function public.can_send_message(uuid) is
  'Guide steps 33/34/36. The WORKER-side pre-send gate, and the only gate a worker '
  'may act on. Delegates checks 0-4 (contact exists, global suppression, consent '
  'ledger, sticky sender active, sticky-sender match) to can_send() without '
  'duplicating a line of that logic, and re-evaluates only the per-conversation '
  'FIFO check with the target message excluded - because can_send() counts the '
  'very message being sent as in-flight and so can never approve a real send. '
  'Approves only the FIFO head: no other outbound message in the conversation may '
  'be sending, and none may be queued ahead of it. Every other refusal from '
  'can_send() is returned unchanged. Takes only a message id - never widen this to '
  'accept a sender override (guide step 32).';

revoke all on function public.can_send_message(uuid) from public;
revoke all on function public.can_send_message(uuid) from anon, authenticated;

commit;
