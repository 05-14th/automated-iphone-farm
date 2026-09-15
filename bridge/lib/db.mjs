// db.mjs - Supabase service-role client plus the handful of data operations the
// worker and the API share.
//
// service_role has BYPASSRLS, which is exactly why every one of the seven tables
// carries a deny-all policy (see docs/BRIDGE-SCHEMA.md §8): the ONLY way into
// this data is a trusted server-side process holding this key.

import { createClient } from '@supabase/supabase-js';
import config from './env.mjs';

export const db = createClient(config.supabaseUrl, config.supabaseServiceRoleKey, {
  auth: { persistSession: false, autoRefreshToken: false },
  global: { headers: { 'x-bridge-client': 'imessage-bridge' } },
});

/** Postgres unique-violation. Defects A and B both surface here as a *normal* outcome. */
export const UNIQUE_VIOLATION = '23505';
export const isUniqueViolation = (err) => err?.code === UNIQUE_VIOLATION;

/** Check-constraint / trigger raise. The state machine uses errcode 23514. */
export const CHECK_VIOLATION = '23514';

export function throwIf(error, context) {
  if (error) {
    const e = new Error(`${context}: ${error.message}`);
    e.code = error.code;
    e.details = error.details;
    e.cause = error;
    throw e;
  }
}

// ---------------------------------------------------------------------------
// normalize_address - the canonical key for every suppression and routing check.
// Mirrors public.normalize_address() so the API can answer a lookup without a
// round trip. The DATABASE remains authoritative: contacts and suppressions both
// re-normalize on write via their BEFORE triggers, so a divergence here can
// never produce a wrongly-keyed row, only a wrong cache lookup.
// ---------------------------------------------------------------------------
export function normalizeAddress(raw) {
  if (raw === null || raw === undefined) return null;
  const trimmed = String(raw).trim();
  if (trimmed === '') return null;
  if (trimmed.includes('@')) return trimmed.toLowerCase();
  const digits = trimmed.replace(/[^0-9]/g, '');
  if (digits === '') return trimmed.toLowerCase();
  if (trimmed.startsWith('+')) return `+${digits}`;
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith('1')) return `+${digits}`;
  return `+${digits}`;
}

// ---------------------------------------------------------------------------
// can_send - the single pre-send gate. Never bypassed, never widened.
// ---------------------------------------------------------------------------

/** Pre-enqueue gate: is this contact reachable at all right now? */
export async function canSend(contactId) {
  const { data, error } = await db.rpc('can_send', { p_contact_id: contactId });
  throwIf(error, 'can_send');
  return data;
}

/**
 * Worker gate: may THIS queued message go out right now?
 *
 * can_send() refuses whenever anything is queued or sending on the conversation -
 * which includes the message the worker is about to send, so can_send() alone can
 * never approve a real send. can_send_message() (migration 20260915...) calls
 * can_send() for checks 1-4 verbatim and only re-evaluates check 5 with the
 * target message excluded, requiring it to be the FIFO head. The suppression,
 * consent, sender-status and sticky-sender logic is not duplicated anywhere.
 */
export async function canSendMessage(messageId) {
  const { data, error } = await db.rpc('can_send_message', { p_message_id: messageId });
  throwIf(error, 'can_send_message');
  return data;
}

/**
 * Operating caps + quiet hours for one sender, with current usage [guide step 36].
 *
 * The same `sender_cap_status()` the gate itself calls, so an operator display
 * and the gate's verdict can never disagree. Read-only; it decides nothing.
 */
export async function senderCapStatus(senderId) {
  const { data, error } = await db.rpc('sender_cap_status', { p_sender_id: senderId });
  throwIf(error, 'sender_cap_status');
  return data;
}

// ---------------------------------------------------------------------------
// senders
// ---------------------------------------------------------------------------
export async function getSenders({ onlyActive = false } = {}) {
  let q = db.from('senders').select('*').order('slug');
  if (onlyActive) q = q.eq('status', 'active');
  const { data, error } = await q;
  throwIf(error, 'getSenders');
  return data ?? [];
}

export async function getSenderBySlug(slug) {
  const { data, error } = await db.from('senders').select('*').eq('slug', slug).maybeSingle();
  throwIf(error, 'getSenderBySlug');
  return data;
}

/**
 * Flip a sender's status. senders_paused_reason_chk requires a reason for any
 * non-active status, so `reason` is mandatory unless status === 'active'.
 */
export async function setSenderStatus(senderId, status, reason) {
  const patch = { status, paused_reason: status === 'active' ? null : reason };
  const { data, error } = await db
    .from('senders')
    .update(patch)
    .eq('id', senderId)
    .select()
    .maybeSingle();
  throwIf(error, 'setSenderStatus');
  return data;
}

// ---------------------------------------------------------------------------
// contacts / conversations - sticky routing lives here
// ---------------------------------------------------------------------------
export async function getContactByAddress(address) {
  const normalized = normalizeAddress(address);
  if (!normalized) return null;
  const { data, error } = await db
    .from('contacts')
    .select('*')
    .eq('normalized_address', normalized)
    .maybeSingle();
  throwIf(error, 'getContactByAddress');
  return data;
}

/**
 * Resolve a contact, creating it pinned to `defaultSenderId` if it is new.
 *
 * Sticky routing (guide step 32) is absolute: an EXISTING contact is returned
 * with its original sticky sender regardless of what the caller asked for. A
 * caller that names a different sender gets `stickyMismatch: true` and must be
 * refused - never silently rerouted, never silently honoured.
 */
export async function resolveContact(rawAddress, defaultSenderId, displayName) {
  const normalized = normalizeAddress(rawAddress);
  if (!normalized) throw new Error(`Address does not normalize: ${rawAddress}`);

  const existing = await getContactByAddress(normalized);
  if (existing) {
    return { contact: existing, created: false, stickyMismatch: existing.sticky_sender_id !== defaultSenderId };
  }

  const { data, error } = await db
    .from('contacts')
    .insert({
      raw_address: String(rawAddress).trim(),
      normalized_address: normalized,
      display_name: displayName ?? null,
      sticky_sender_id: defaultSenderId,
    })
    .select()
    .maybeSingle();

  if (isUniqueViolation(error)) {
    // Concurrent create for the same address. The other writer won; its sticky
    // sender is now the truth.
    const raced = await getContactByAddress(normalized);
    return { contact: raced, created: false, stickyMismatch: raced.sticky_sender_id !== defaultSenderId };
  }
  throwIf(error, 'resolveContact.insert');
  return { contact: data, created: true, stickyMismatch: false };
}

export async function resolveConversation(contact) {
  const { data: found, error: selErr } = await db
    .from('conversations')
    .select('*')
    .eq('contact_id', contact.id)
    .eq('sender_id', contact.sticky_sender_id)
    .maybeSingle();
  throwIf(selErr, 'resolveConversation.select');
  if (found) return { conversation: found, created: false };

  const { data, error } = await db
    .from('conversations')
    .insert({ contact_id: contact.id, sender_id: contact.sticky_sender_id })
    .select()
    .maybeSingle();

  if (isUniqueViolation(error)) {
    const { data: raced, error: e2 } = await db
      .from('conversations')
      .select('*')
      .eq('contact_id', contact.id)
      .eq('sender_id', contact.sticky_sender_id)
      .maybeSingle();
    throwIf(e2, 'resolveConversation.race');
    return { conversation: raced, created: false };
  }
  throwIf(error, 'resolveConversation.insert');
  return { conversation: data, created: true };
}

export async function setConversationChatGuid(conversationId, chatGuid) {
  const { data, error } = await db
    .from('conversations')
    .update({ provider_chat_guid: chatGuid })
    .eq('id', conversationId)
    .is('provider_chat_guid', null)
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) {
    // Another conversation already owns this provider chat. That is a data
    // integrity alarm, not something to paper over - surface it.
    const e = new Error(`provider_chat_guid ${chatGuid} is already attached to another conversation`);
    e.code = UNIQUE_VIOLATION;
    throw e;
  }
  throwIf(error, 'setConversationChatGuid');
  return data;
}

// ---------------------------------------------------------------------------
// messages
// ---------------------------------------------------------------------------
export function newTempGuid() {
  return `temp-${crypto.randomUUID()}`;
}

export async function enqueueOutbound({ conversation, senderId, body }) {
  const { data, error } = await db
    .from('messages')
    .insert({
      conversation_id: conversation.id,
      sender_id: senderId,
      direction: 'outbound',
      body,
      state: 'queued',
      temp_guid: newTempGuid(),
    })
    .select()
    .maybeSingle();
  throwIf(error, 'enqueueOutbound');
  return data;
}

export async function getMessage(id) {
  const { data, error } = await db.from('messages').select('*').eq('id', id).maybeSingle();
  throwIf(error, 'getMessage');
  return data;
}

export async function getAttempts(messageId) {
  const { data, error } = await db
    .from('send_attempts')
    .select('*')
    .eq('message_id', messageId)
    .order('attempt_no');
  throwIf(error, 'getAttempts');
  return data ?? [];
}

export async function recordAttempt(messageId, { httpStatus, response, outcome }) {
  const prior = await getAttempts(messageId);
  const attemptNo = prior.length ? Math.max(...prior.map((a) => a.attempt_no)) + 1 : 1;
  const { data, error } = await db
    .from('send_attempts')
    .insert({
      message_id: messageId,
      attempt_no: attemptNo,
      http_status: httpStatus ?? null,
      response: response ?? null,
      outcome,
    })
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) {
    // Two processes numbered the same attempt. Retry once with a fresh number.
    const again = await getAttempts(messageId);
    const n = again.length ? Math.max(...again.map((a) => a.attempt_no)) + 1 : 1;
    const { data: d2, error: e2 } = await db
      .from('send_attempts')
      .insert({ message_id: messageId, attempt_no: n, http_status: httpStatus ?? null, response: response ?? null, outcome })
      .select()
      .maybeSingle();
    throwIf(e2, 'recordAttempt.retry');
    return d2;
  }
  throwIf(error, 'recordAttempt');
  return data;
}

/**
 * Atomically claim a queued message: queued -> sending.
 *
 * Two guards do the work, both in the database:
 *   - `.eq('state', 'queued')` makes the UPDATE a compare-and-swap, so a second
 *     worker's update matches zero rows.
 *   - messages_one_sending_per_conversation_uidx raises 23505 if another message
 *     in the same conversation is already sending.
 * Either way the caller gets `null` and moves on. Losing a claim is routine,
 * not an error.
 *
 * `messages.was_first_contact` is NOT set here. The database stamps it on this
 * exact transition (trg_messages_stamp_first_contact), reading the
 * conversation's provider_chat_guid itself, so the new-conversation cap counts
 * the same thing no matter which process did the claiming and a worker cannot
 * forget to record it. The claimed row comes back with the flag already on it.
 */
export async function claimMessage(messageId) {
  const { data, error } = await db
    .from('messages')
    .update({ state: 'sending', sending_at: new Date().toISOString() })
    .eq('id', messageId)
    .eq('state', 'queued')
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) return null;
  throwIf(error, 'claimMessage');
  return data;
}

export async function markSent(messageId, { providerGuid, deliveredAt, reconciled }) {
  const patch = { state: 'sent', sent_at: new Date().toISOString() };
  if (providerGuid) patch.provider_guid = providerGuid;
  if (deliveredAt) patch.delivered_at = deliveredAt;
  if (reconciled) patch.reconciled = true;

  const { data, error } = await db.from('messages').update(patch).eq('id', messageId).select().maybeSingle();
  if (isUniqueViolation(error)) {
    // Defect B: the echo webhook already claimed this provider_guid. Record the
    // state transition without the guid rather than failing the send.
    delete patch.provider_guid;
    const { data: d2, error: e2 } = await db
      .from('messages')
      .update(patch)
      .eq('id', messageId)
      .select()
      .maybeSingle();
    throwIf(e2, 'markSent.withoutGuid');
    return d2;
  }
  throwIf(error, 'markSent');
  return data;
}

export async function markFailed(messageId, { errorCode, reconciled = false } = {}) {
  const { data, error } = await db
    .from('messages')
    .update({
      state: 'failed',
      failed_at: new Date().toISOString(),
      error_code: errorCode ? String(errorCode) : null,
      reconciled,
    })
    .eq('id', messageId)
    .select()
    .maybeSingle();
  throwIf(error, 'markFailed');
  return data;
}

/** failed -> sent. The database refuses this unless reconciled = true. */
export async function reconcileToSent(messageId, { providerGuid, deliveredAt }) {
  const { data, error } = await db
    .from('messages')
    .update({
      state: 'sent',
      reconciled: true,
      provider_guid: providerGuid ?? undefined,
      sent_at: new Date().toISOString(),
      delivered_at: deliveredAt ?? null,
    })
    .eq('id', messageId)
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) {
    const { data: d2, error: e2 } = await db
      .from('messages')
      .update({ state: 'sent', reconciled: true, sent_at: new Date().toISOString() })
      .eq('id', messageId)
      .select()
      .maybeSingle();
    throwIf(e2, 'reconcileToSent.withoutGuid');
    return d2;
  }
  throwIf(error, 'reconcileToSent');
  return data;
}

/** failed -> failed, but now reconciled = true: proven NOT delivered, safe to requeue. */
export async function markReconciledNotDelivered(messageId) {
  const { data, error } = await db
    .from('messages')
    .update({ reconciled: true })
    .eq('id', messageId)
    .select()
    .maybeSingle();
  throwIf(error, 'markReconciledNotDelivered');
  return data;
}

/** The claim scan: oldest queued outbound messages for a sender, FIFO by queued_at. */
export async function claimableMessages(senderId, limit = 25) {
  const { data, error } = await db
    .from('messages')
    .select('*, conversations!inner(id, provider_chat_guid, contact_id)')
    .eq('sender_id', senderId)
    .eq('state', 'queued')
    .eq('direction', 'outbound')
    // Scheduled sends: a NULL scheduled_for means "send now" (the original
    // behaviour). A future timestamp keeps the row invisible until it is due.
    // The timestamp MUST be computed per call - a module-load value would
    // freeze and the worker would silently stop claiming anything.
    .or(`scheduled_for.is.null,scheduled_for.lte.${new Date().toISOString()}`)
    .order('queued_at', { ascending: true })
    .limit(limit);
  throwIf(error, 'claimableMessages');
  return data ?? [];
}

// ---------------------------------------------------------------------------
// suppressions / consents
// ---------------------------------------------------------------------------
export async function getSuppression(address) {
  const normalized = normalizeAddress(address);
  if (!normalized) return null;
  const { data, error } = await db
    .from('suppressions')
    .select('*')
    .eq('normalized_address', normalized)
    .maybeSingle();
  throwIf(error, 'getSuppression');
  return data;
}

/** Idempotent: a second STOP from the same person is a no-op, not an error. */
export async function addSuppression(rawAddress, reason) {
  const normalized = normalizeAddress(rawAddress);
  if (!normalized) throw new Error(`Address does not normalize: ${rawAddress}`);
  const { data, error } = await db
    .from('suppressions')
    .insert({ raw_address: String(rawAddress).trim(), normalized_address: normalized, reason })
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) {
    return { suppression: await getSuppression(normalized), created: false };
  }
  throwIf(error, 'addSuppression');
  return { suppression: data, created: true };
}

export async function latestConsent(contactId) {
  const { data, error } = await db
    .from('consents')
    .select('*')
    .eq('contact_id', contactId)
    .order('created_at', { ascending: false })
    .order('id', { ascending: false })
    .limit(1);
  throwIf(error, 'latestConsent');
  return data?.[0] ?? null;
}

export async function recordConsent({ contactId, status, source, evidence }) {
  const now = new Date().toISOString();
  const row = {
    contact_id: contactId,
    status,
    source,
    evidence: evidence ?? {},
    granted_at: status === 'granted' ? now : null,
    revoked_at: status === 'revoked' ? now : null,
  };
  const { data, error } = await db.from('consents').insert(row).select().maybeSingle();
  throwIf(error, 'recordConsent');
  return data;
}

// ---------------------------------------------------------------------------
// inbound ingest - Defect B collapses here
// ---------------------------------------------------------------------------

/**
 * Insert an inbound message, deduped by provider_guid.
 *
 * messages_provider_guid_uidx raises 23505 on the duplicate webhook. That is the
 * EXPECTED path (measured: 8 of 11 outbound sends duplicate), so it returns
 * `{ deduped: true }` rather than throwing.
 */
export async function ingestInbound({ conversationId, senderId, providerGuid, body, deliveredAt }) {
  const { data, error } = await db
    .from('messages')
    .insert({
      conversation_id: conversationId,
      sender_id: senderId,
      direction: 'inbound',
      body,
      state: 'sent',
      provider_guid: providerGuid,
      sent_at: deliveredAt ?? new Date().toISOString(),
      delivered_at: deliveredAt ?? null,
    })
    .select()
    .maybeSingle();

  if (isUniqueViolation(error)) return { message: null, deduped: true };
  throwIf(error, 'ingestInbound');
  return { message: data, deduped: false };
}

/** Attach a provider_guid to an outbound row, deduping on the echo webhook. */
export async function attachProviderGuid(messageId, providerGuid, extra = {}) {
  const { data, error } = await db
    .from('messages')
    .update({ provider_guid: providerGuid, ...extra })
    .eq('id', messageId)
    .is('provider_guid', null)
    .select()
    .maybeSingle();
  if (isUniqueViolation(error)) return { message: null, deduped: true };
  throwIf(error, 'attachProviderGuid');
  return { message: data, deduped: false };
}

export async function findMessageByProviderGuid(providerGuid) {
  const { data, error } = await db
    .from('messages')
    .select('*')
    .eq('provider_guid', providerGuid)
    .maybeSingle();
  throwIf(error, 'findMessageByProviderGuid');
  return data;
}

export async function findMessageByTempGuid(tempGuid) {
  const { data, error } = await db.from('messages').select('*').eq('temp_guid', tempGuid).maybeSingle();
  throwIf(error, 'findMessageByTempGuid');
  return data;
}

export async function findConversationByChatGuid(chatGuid) {
  const { data, error } = await db
    .from('conversations')
    .select('*')
    .eq('provider_chat_guid', chatGuid)
    .maybeSingle();
  throwIf(error, 'findConversationByChatGuid');
  return data;
}

export async function queueDepth() {
  const { count: queued, error: e1 } = await db
    .from('messages')
    .select('id', { count: 'exact', head: true })
    .eq('state', 'queued')
    .eq('direction', 'outbound');
  throwIf(e1, 'queueDepth.queued');

  const { count: sending, error: e2 } = await db
    .from('messages')
    .select('id', { count: 'exact', head: true })
    .eq('state', 'sending');
  throwIf(e2, 'queueDepth.sending');

  const { count: unreconciled, error: e3 } = await db
    .from('messages')
    .select('id', { count: 'exact', head: true })
    .eq('state', 'failed')
    .eq('reconciled', false);
  throwIf(e3, 'queueDepth.unreconciled');

  return { queued: queued ?? 0, sending: sending ?? 0, unreconciled_failed: unreconciled ?? 0 };
}

export default db;
