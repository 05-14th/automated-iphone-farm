-- Phase 11 - Row Level Security: deny by default on every bridge table.
--
-- SERVICE-ROLE BYPASS
-- ------------------
-- Supabase's `service_role` is created with the BYPASSRLS attribute, so it is not subject
-- to any policy below. The bridge worker and the webhook ingest run as service_role and
-- therefore keep full access WITHOUT any policy being written for them. That is deliberate:
-- the only way to reach this data is the trusted server-side worker.
--
-- Every table below is therefore: RLS enabled + one explicit deny-all policy. The deny-all
-- policy is redundant with "RLS on and no policies" but is written out so that a future
-- `create policy` cannot accidentally widen access by being the only policy on the table -
-- a reviewer sees an explicit deny sitting next to it.
--
-- No auth roles are invented here. If portal users ever need direct read access, that is a
-- separate, reviewed migration; until then the portal reads through the worker
-- (guide step 35: "render the portal from the database" - via the service-role API, not by
-- handing browsers a key).

begin;

alter table public.senders       enable row level security;
alter table public.contacts      enable row level security;
alter table public.conversations enable row level security;
alter table public.messages      enable row level security;
alter table public.consents      enable row level security;
alter table public.suppressions  enable row level security;
alter table public.send_attempts enable row level security;

create policy senders_deny_all       on public.senders       for all to public using (false) with check (false);
create policy contacts_deny_all      on public.contacts      for all to public using (false) with check (false);
create policy conversations_deny_all on public.conversations for all to public using (false) with check (false);
create policy messages_deny_all      on public.messages      for all to public using (false) with check (false);
create policy consents_deny_all      on public.consents      for all to public using (false) with check (false);
create policy suppressions_deny_all  on public.suppressions  for all to public using (false) with check (false);
create policy send_attempts_deny_all on public.send_attempts for all to public using (false) with check (false);

comment on policy senders_deny_all       on public.senders       is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy contacts_deny_all      on public.contacts      is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy conversations_deny_all on public.conversations is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy messages_deny_all      on public.messages      is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy consents_deny_all      on public.consents      is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy suppressions_deny_all  on public.suppressions  is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';
comment on policy send_attempts_deny_all on public.send_attempts is 'Deny by default. service_role has BYPASSRLS and is the only intended accessor.';

-- Belt and braces: no table-level grants to the browser-facing roles either, so an
-- accidentally dropped policy still does not expose anything.
revoke all on public.senders, public.contacts, public.conversations, public.messages,
               public.consents, public.suppressions, public.send_attempts
  from anon, authenticated;

commit;
