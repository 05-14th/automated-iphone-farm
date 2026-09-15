-- Phase 14 [guide step 36] - operating caps and quiet hours.
--
-- WHY THIS EXISTS
--
-- Until this migration nothing limited total outbound VOLUME. The worker paced
-- sends 8-12 s apart, which caps the *rate* but not the *day*: left alone, one
-- sender would emit ~7,000 messages in 24 h, at every hour of the night, from a
-- young Apple Account. That is the single most likely way to get the account
-- shut down, and no amount of pacing prevents it.
--
-- Caps are per-sender configuration, not code, because the right number differs
-- per identity and changes as an account ages. They live on public.senders so an
-- operator can raise or lower one with an UPDATE and the change is picked up by
-- the very next gate call - no deploy, no restart.
--
-- A NULL cap means "no limit". Every cap ships with a non-null DEFAULT, so the
-- existing sender rows are capped the moment this migration lands rather than
-- staying unlimited until someone remembers to configure them. Opting OUT of a
-- cap is the deliberate act; being capped is the default.
--
-- A cap refusal is NOT a failure. It is "not yet". The worker leaves the message
-- queued and it goes out when the window reopens. Only suppression and revoked
-- consent - refusals that can never clear on their own - fail a message.

begin;

-- ---------------------------------------------------------------------------
-- 0. timezone validation helper
-- ---------------------------------------------------------------------------
-- Declared IMMUTABLE so it may back a CHECK constraint. Strictly it is STABLE
-- (the IANA tz database can gain a zone on a server upgrade), but a zone name
-- that is valid today does not become invalid tomorrow, so the direction that
-- matters for a constraint - "accepted rows stay valid" - does hold.
create or replace function public.is_valid_timezone(p_tz text)
returns boolean
language plpgsql
immutable
set search_path = pg_catalog, public
as $fn$
begin
  if p_tz is null then
    return true;
  end if;
  perform timestamptz '2000-01-01 00:00:00+00' at time zone p_tz;
  return true;
exception
  when others then
    return false;
end;
$fn$;

comment on function public.is_valid_timezone(text) is
  'True if p_tz is a zone Postgres can resolve (NULL counts as valid). Exists so '
  'senders_timezone_chk can reject a typo like "America/Vancover" at write time instead of '
  'letting quiet hours silently evaluate in the wrong zone - or in UTC, which for a Pacific '
  'sender is an 7-8 hour error and would send at 1 a.m. local.';

-- ---------------------------------------------------------------------------
-- 1. the cap columns [guide step 36]
-- ---------------------------------------------------------------------------
alter table public.senders
  add column if not exists daily_cap                  integer default 200,
  add column if not exists hourly_cap                 integer default 30,
  add column if not exists new_conversation_daily_cap integer default 20,
  add column if not exists quiet_hours_start          time default '21:00',
  add column if not exists quiet_hours_end            time default '08:00',
  add column if not exists timezone                   text default 'America/Vancouver';

comment on column public.senders.daily_cap is
  'Maximum outbound messages this sender may send in any ROLLING 24 h window. NULL = no limit. '
  'Default 200. Reasoning: at the worker''s 8-12 s pacing 200 messages occupy roughly 30-40 '
  'minutes of actual sending, so the cap binds on volume rather than fighting the pacer; it is '
  'an order of magnitude below the ~7,000/day the pacer alone would permit; and it is inside the '
  'range a human-operated iMessage account plausibly produces, which is the only bar Apple can '
  'actually measure. It is a ceiling, not a target - a new identity should be run far lower and '
  'raised as it ages.';
comment on column public.senders.hourly_cap is
  'Maximum outbound messages in any ROLLING 60 min window. NULL = no limit. Default 30, i.e. one '
  'every two minutes sustained. This is the BURST guard, and it is the cap that does the real '
  'work: the daily cap alone would allow all 200 messages inside 40 minutes, which is exactly '
  'the shape that looks automated. 30/h also keeps a full daily allowance spread over most of a '
  'working day.';
comment on column public.senders.new_conversation_daily_cap is
  'Maximum FIRST-CONTACT messages in any rolling 24 h window. NULL = no limit. Default 20. '
  'First contact is materially riskier than replying into an existing thread: it is the only '
  'outbound event a recipient who never asked to hear from us can report, it takes the slow '
  'chat/new path, and a burst of them to strangers is the precise signature of the behaviour '
  'Apple bans accounts for. Capped separately and an order of magnitude tighter than daily_cap '
  'on purpose - a sender may spend its whole day in conversation and still open only 20 new '
  'ones.';
comment on column public.senders.quiet_hours_start is
  'Start of the DO-NOT-SEND window in the sender''s local time (senders.timezone). NULL (with '
  'quiet_hours_end NULL) = always allowed. Default 21:00. A window whose start is later than its '
  'end wraps midnight, which is the normal case.';
comment on column public.senders.quiet_hours_end is
  'End of the do-not-send window, i.e. when sending resumes, in the sender''s local time. '
  'Default 08:00, so the default quiet window is 21:00-08:00 local.';
comment on column public.senders.timezone is
  'IANA zone the quiet-hours window is evaluated in. NOT decorative: quiet hours evaluated in '
  'UTC for a Pacific sender would send at 1 a.m. local. Default America/Vancouver (the operator''s '
  'own zone). Validated by senders_timezone_chk.';

-- A cap of 0 is meaningful ("this sender sends nothing right now") and is kept
-- legal deliberately; a negative cap is a typo.
alter table public.senders drop constraint if exists senders_caps_non_negative_chk;
alter table public.senders add constraint senders_caps_non_negative_chk
  check (
    coalesce(daily_cap, 0) >= 0
    and coalesce(hourly_cap, 0) >= 0
    and coalesce(new_conversation_daily_cap, 0) >= 0
  );

-- Half a quiet-hours window is not a window. Either both ends are set or neither.
alter table public.senders drop constraint if exists senders_quiet_hours_pair_chk;
alter table public.senders add constraint senders_quiet_hours_pair_chk
  check ((quiet_hours_start is null) = (quiet_hours_end is null));

-- Quiet hours without a zone would be evaluated in UTC and silently send at the
-- wrong local hour. Refuse the configuration instead.
alter table public.senders drop constraint if exists senders_quiet_hours_timezone_chk;
alter table public.senders add constraint senders_quiet_hours_timezone_chk
  check (quiet_hours_start is null or timezone is not null);

alter table public.senders drop constraint if exists senders_timezone_chk;
alter table public.senders add constraint senders_timezone_chk
  check (public.is_valid_timezone(timezone));

-- ---------------------------------------------------------------------------
-- 2. messages.was_first_contact [guide step 36]
-- ---------------------------------------------------------------------------
-- The new-conversation cap needs to know, for every message already sent,
-- whether it was a first contact. That fact is destroyed the instant the send
-- succeeds: the conversation gains a provider_chat_guid, and every later message
-- looks identical to the first one. So it is RECORDED at send time, never
-- inferred afterwards.
alter table public.messages
  add column if not exists was_first_contact boolean;

comment on column public.messages.was_first_contact is
  'Guide step 36. True if this outbound message opened the conversation - i.e. the conversation '
  'had no provider_chat_guid at the moment the worker claimed it, and the message therefore took '
  'the chat/new path. Stamped by trg_messages_stamp_first_contact on the queued -> sending '
  'transition and never written by hand. NULL means "not sent through the state machine" - a '
  'still-queued message, a pre-flight rejection, an inbound row, or a row that predates this '
  'column - and never counts against the new-conversation cap. It is deliberately NOT inferable '
  'after the fact: once the first send lands, the conversation has a chat GUID and every '
  'subsequent message looks exactly like the first.';

alter table public.messages drop constraint if exists messages_first_contact_outbound_chk;
alter table public.messages add constraint messages_first_contact_outbound_chk
  check (was_first_contact is null or direction = 'outbound');

-- The stamping trigger. It runs on the SAME update that claims the message, so
-- the flag and the state change are one atomic fact. A worker cannot forget to
-- set it, and neither can any other writer - which is the point: the Python API,
-- a manual SQL fix and the Node worker all get identical accounting.
create or replace function public.tg_messages_stamp_first_contact()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $fn$
declare
  v_chat_guid text;
begin
  if new.state = 'sending' and old.state = 'queued' and new.direction = 'outbound' then
    select cv.provider_chat_guid into v_chat_guid
    from public.conversations cv
    where cv.id = new.conversation_id;

    new.was_first_contact := (v_chat_guid is null);
  end if;
  return new;
end;
$fn$;

comment on function public.tg_messages_stamp_first_contact() is
  'Guide step 36. Records, at claim time, whether the message being sent is opening the '
  'conversation. Fires on queued -> sending only, because that is the exact instant the fact is '
  'still knowable. Deliberately overwrites whatever the caller supplied: the database observes '
  'this, it does not accept a claim about it.';

drop trigger if exists trg_messages_stamp_first_contact on public.messages;
create trigger trg_messages_stamp_first_contact
  before update of state on public.messages
  for each row execute function public.tg_messages_stamp_first_contact();

-- The cap-counting scan. The partial predicate IS the counting rule (see
-- sender_cap_window below), so the index and the rule cannot drift apart.
create index if not exists messages_cap_window_idx
  on public.messages (
    sender_id,
    (coalesce(sending_at, sent_at, queued_at))
  )
  where direction = 'outbound'
    and (
      state in ('sending', 'sent')
      or (state = 'failed' and reconciled = true and provider_guid is not null)
    );

comment on index public.messages_cap_window_idx is
  'Guide step 36. Serves every rolling-window cap count. Its WHERE clause is the counting rule '
  'itself: a message consumes cap allowance once it has actually been handed to the provider '
  '(sending/sent) OR when it is a "failed" row that reconciliation proved DID deliver (Defect A) '
  '- which is the case a naive count(*) where state = ''sent'' silently under-counts. A queued '
  'row is not here, because a queued message has consumed nothing.';

-- ---------------------------------------------------------------------------
-- 3. sender_cap_window - one rolling window, counted once
-- ---------------------------------------------------------------------------
create or replace function public.sender_cap_window(
  p_sender_id         uuid,
  p_window            interval,
  p_cap               integer,
  p_first_contact_only boolean default false
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $fn$
declare
  v_stamps timestamptz[];
  v_used   integer;
  v_retry  timestamptz;
begin
  -- THE COUNTING RULE, in one place.
  --   * outbound only - inbound consumes nothing.
  --   * 'sending' and 'sent' count: both have been handed to BlueBubbles.
  --   * 'queued' does NOT count: nothing has left the Mac, and counting it would
  --     make a backlog permanently block itself.
  --   * 'failed' normally does NOT count - but a failed row with reconciled = true
  --     AND a provider_guid is Defect A's false failure: reconciliation proved it
  --     DID deliver. Apple saw that message, so the cap must see it too.
  --   * the window is rolling from now(), not a calendar day. A calendar cap lets
  --     a sender put 200 messages out at 23:50 and another 200 at 00:10.
  --   * the instant a message consumed its allowance is when it went out, so
  --     sending_at is preferred, falling back to sent_at (a row stamped by a
  --     reconcile that never passed through this worker) and finally queued_at.
  select array_agg(ts order by ts)
    into v_stamps
  from (
    select coalesce(m.sending_at, m.sent_at, m.queued_at) as ts
    from public.messages m
    where m.sender_id = p_sender_id
      and m.direction = 'outbound'
      and (
        m.state in ('sending', 'sent')
        or (m.state = 'failed' and m.reconciled = true and m.provider_guid is not null)
      )
      and (p_first_contact_only is not true or m.was_first_contact is true)
      and coalesce(m.sending_at, m.sent_at, m.queued_at) > now() - p_window
  ) s;

  v_used := coalesce(array_length(v_stamps, 1), 0);

  -- retry_after: the instant this window stops being full. Not simply
  -- "oldest + window" - if the sender is OVER cap (the cap was lowered, or two
  -- workers raced), enough messages have to age out to get back under it. The
  -- element that must expire is the (used - cap + 1)-th oldest.
  if p_cap is not null and v_used >= p_cap then
    v_retry := v_stamps[v_used - p_cap + 1] + p_window;
  end if;

  return jsonb_build_object(
    'cap',              p_cap,
    'used',             v_used,
    'remaining',        case when p_cap is null then null else greatest(p_cap - v_used, 0) end,
    'window_seconds',   extract(epoch from p_window)::bigint,
    'oldest_in_window', v_stamps[1],
    'window_resets_at', case when v_used > 0 then v_stamps[1] + p_window else null end,
    'exceeded',         (p_cap is not null and v_used >= p_cap),
    'retry_after',      v_retry
  );
end;
$fn$;

comment on function public.sender_cap_window(uuid, interval, integer, boolean) is
  'Guide step 36. Counts one rolling cap window for one sender and reports cap/used/remaining '
  'plus retry_after - the instant the window stops being full. The WHERE clause here is the '
  'single definition of what "consumes a cap": outbound, and either sending/sent or a '
  'reconciled-delivered failed row (Defect A''s false failure - Apple saw that message even '
  'though the row says failed). A queued message consumes nothing. Windows roll from now(), '
  'because a calendar-day cap permits 2x the cap across a midnight boundary.';

-- ---------------------------------------------------------------------------
-- 4. sender_cap_status - the whole picture for one sender
-- ---------------------------------------------------------------------------
create or replace function public.sender_cap_status(p_sender_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $fn$
declare
  v_sender    public.senders%rowtype;
  v_tz        text;
  v_local_ts  timestamp;
  v_local_t   time;
  v_in_quiet  boolean := false;
  v_resumes   timestamptz;
  v_end_local timestamp;
begin
  select * into v_sender from public.senders where id = p_sender_id;
  if not found then
    return jsonb_build_object('found', false);
  end if;

  v_tz := coalesce(v_sender.timezone, 'UTC');
  v_local_ts := (now() at time zone v_tz);
  v_local_t := v_local_ts::time;

  if v_sender.quiet_hours_start is not null and v_sender.quiet_hours_end is not null then
    if v_sender.quiet_hours_start = v_sender.quiet_hours_end then
      -- A zero-length window. Treated as "no quiet hours" rather than "silent
      -- forever", because a 24 h quiet window is indistinguishable from
      -- status = 'paused' and should be expressed that way.
      v_in_quiet := false;
    elsif v_sender.quiet_hours_start < v_sender.quiet_hours_end then
      v_in_quiet := v_local_t >= v_sender.quiet_hours_start
                and v_local_t <  v_sender.quiet_hours_end;
    else
      -- Wraps midnight (21:00 -> 08:00), the normal case.
      v_in_quiet := v_local_t >= v_sender.quiet_hours_start
                 or v_local_t <  v_sender.quiet_hours_end;
    end if;

    -- When sending resumes: the next local wall-clock occurrence of
    -- quiet_hours_end, converted back through the sender's zone. Computing this
    -- in local wall-clock time and converting at the end is what makes it
    -- correct across a midnight boundary and across a DST shift; adding an
    -- interval to a timestamptz would not be.
    v_end_local := date_trunc('day', v_local_ts) + v_sender.quiet_hours_end;
    if v_end_local <= v_local_ts then
      v_end_local := v_end_local + interval '1 day';
    end if;
    v_resumes := v_end_local at time zone v_tz;
  end if;

  return jsonb_build_object(
    'found',      true,
    'sender_id',  v_sender.id,
    'slug',       v_sender.slug,
    'status',     v_sender.status,
    'checked_at', now(),
    'timezone',   v_sender.timezone,
    'local_time', to_char(v_local_ts, 'YYYY-MM-DD HH24:MI:SS'),
    'quiet_hours', jsonb_build_object(
      'start',          v_sender.quiet_hours_start,
      'end',            v_sender.quiet_hours_end,
      'timezone',       v_sender.timezone,
      'configured',     (v_sender.quiet_hours_start is not null),
      'in_quiet_hours', v_in_quiet,
      'resumes_at',     case when v_in_quiet then v_resumes else null end,
      'next_quiet_end', v_resumes
    ),
    'daily',  public.sender_cap_window(v_sender.id, interval '24 hours', v_sender.daily_cap, false),
    'hourly', public.sender_cap_window(v_sender.id, interval '1 hour',  v_sender.hourly_cap, false),
    'new_conversation_daily',
              public.sender_cap_window(v_sender.id, interval '24 hours', v_sender.new_conversation_daily_cap, true)
  );
end;
$fn$;

comment on function public.sender_cap_status(uuid) is
  'Guide step 36. Every operating cap for one sender with its current usage, remaining headroom, '
  'window reset time and retry_after, plus the quiet-hours verdict evaluated in the SENDER''S '
  'timezone. Read by can_send() so the gate and the operator-facing GET /v1/senders can never '
  'disagree about where a sender stands.';

-- ---------------------------------------------------------------------------
-- 5. can_send() - caps folded into the existing gate
-- ---------------------------------------------------------------------------
-- The added checks sit AFTER suppression and consent and BEFORE the in-flight
-- check. That position is deliberate:
--   * after suppression/consent, because a suppressed contact must never be
--     reported as merely "rate limited" - that reads as "try again tomorrow",
--     which is precisely the wrong thing to do;
--   * before the in-flight check, because in_flight_message_exists is a
--     millisecond-scale ordering condition while a cap is an hours-scale policy
--     one, and the caller needs to be told about the one that actually governs.
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
  v_has_conv     boolean;
  v_first        boolean;
  v_caps         jsonb;
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
  v_has_conv := found;

  if v_has_conv and v_conv.sender_id <> v_contact.sticky_sender_id then
    return jsonb_build_object('allowed', false, 'reason', 'sticky_sender_mismatch');
  end if;

  -- 5. OPERATING CAPS AND QUIET HOURS [guide step 36].
  --    Every refusal here is TEMPORARY and carries a retry_after. A caller must
  --    keep the message queued and let it go out when the window reopens -
  --    failing it would drop traffic that policy merely deferred.
  --
  --    A message opens a conversation when there is no conversation yet, or when
  --    the one that exists has no provider_chat_guid (so the send will take the
  --    chat/new path). That is the same condition the worker branches on, and
  --    the same one trg_messages_stamp_first_contact records.
  v_first := (not v_has_conv) or (v_conv.provider_chat_guid is null);
  v_caps := public.sender_cap_status(v_sender.id);

  -- 5a. Quiet hours first: it is the blanket gate. Being inside it makes every
  --     cap question moot, and its retry_after is the honest one to report.
  if (v_caps #>> '{quiet_hours,in_quiet_hours}')::boolean then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'quiet_hours',
      'retry_after', v_caps #> '{quiet_hours,resumes_at}',
      'detail', v_caps -> 'quiet_hours'
    );
  end if;

  -- 5b. Daily before hourly: both may be blown at once, and the daily window is
  --     the one that frees up last. Reporting the hourly retry_after while the
  --     day is spent would promise a send that will be refused again in an hour.
  if (v_caps #>> '{daily,exceeded}')::boolean then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'cap_daily',
      'retry_after', v_caps #> '{daily,retry_after}',
      'detail', v_caps -> 'daily'
    );
  end if;

  if (v_caps #>> '{hourly,exceeded}')::boolean then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'cap_hourly',
      'retry_after', v_caps #> '{hourly,retry_after}',
      'detail', v_caps -> 'hourly'
    );
  end if;

  -- 5c. The new-conversation cap is last because it is the narrowest: it refuses
  --     only first contacts, and a sender that has spent it can still reply into
  --     every thread it already has open.
  if v_first and (v_caps #>> '{new_conversation_daily,exceeded}')::boolean then
    return jsonb_build_object(
      'allowed', false,
      'reason', 'cap_new_conversation',
      'retry_after', v_caps #> '{new_conversation_daily,retry_after}',
      'detail', v_caps -> 'new_conversation_daily'
    );
  end if;

  -- 6. IN-FLIGHT MESSAGE. Guide step 34 (per-conversation FIFO): one message at a time in
  --    a conversation. This is also the second line of defence against Defect A - while a
  --    send whose outcome is unknown is still queued or sending, nothing else may go out on
  --    that thread and compound the ambiguity.
  if v_has_conv then
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
  'Guide step 36. The single pre-send gate. Returns {"allowed": bool, "reason": text} - plus '
  '"retry_after" and "detail" on a temporary refusal - and evaluates in this fixed order, '
  'returning on the first failure: '
  '(1) suppression - global across all senders, outranks consent; '
  '(2) valid consent - newest row in the append-only consents ledger must be granted; '
  '(3) sender paused - a non-active sticky sender halts traffic, it is never rerouted (step 32); '
  '(4) sticky-sender match - an existing conversation must belong to the contact''s first sender; '
  '(5) operating caps and quiet hours - quiet_hours, cap_daily, cap_hourly, cap_new_conversation, '
  'each carrying a retry_after; '
  '(6) in-flight message - per-conversation FIFO (step 34), and a guard against piling new sends '
  'on top of a send whose outcome is still unknown (Defect A). '
  'Order matters: a suppressed contact must never be evaluated for consent or reported as merely '
  'rate-limited, a paused sender must never be evaluated for an alternative, and an hours-scale '
  'cap must be reported ahead of a millisecond-scale FIFO condition. '
  'The (5) refusals are TEMPORARY: a caller must leave the message queued, not fail it. Only (1) '
  'and a revoked (2) can never clear on their own. '
  'SECURITY DEFINER so the worker gets a truthful verdict regardless of the caller''s RLS view - '
  'never widen this to accept a sender override.';

revoke all on function public.can_send(uuid) from public;
revoke all on function public.can_send(uuid) from anon, authenticated;
revoke all on function public.sender_cap_window(uuid, interval, integer, boolean) from public;
revoke all on function public.sender_cap_window(uuid, interval, integer, boolean) from anon, authenticated;
revoke all on function public.sender_cap_status(uuid) from public;
revoke all on function public.sender_cap_status(uuid) from anon, authenticated;

commit;
