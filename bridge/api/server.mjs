#!/usr/bin/env node
// server.mjs - the gateway other systems call. node:http only, no framework.
//
// Binds 127.0.0.1 by default. Every /v1/* route except the BlueBubbles webhook
// requires a bearer token; the webhook route is exempt because BlueBubbles cannot
// be configured to send an Authorization header, and it is reachable only from
// loopback.
//
// The API never sends anything. It enqueues, and the worker sends. That
// separation is what makes the per-conversation FIFO rule and the rate limit
// enforceable at all.

import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import config from '../lib/env.mjs';
import { createLogger } from '../lib/log.mjs';
import * as dbapi from '../lib/db.mjs';

const log = createLogger('api');
const MAX_BODY = 1024 * 1024;

// STOP keywords. Deliberately conservative: an exact match on the whole trimmed
// message, not a substring. "I'll stop by tomorrow" must not opt someone out,
// and a false positive here silently kills a real conversation.
const STOP_KEYWORDS = new Set([
  'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit', 'optout', 'opt-out', 'opt out', 'remove', 'revoke',
]);

export function isStopMessage(text) {
  if (typeof text !== 'string') return false;
  const cleaned = text
    .trim()
    .toLowerCase()
    .replace(/[\s​]+/g, ' ')
    .replace(/^[\p{P}\p{S}]+|[\p{P}\p{S}]+$/gu, '');
  if (!cleaned) return false;
  return STOP_KEYWORDS.has(cleaned);
}

// ---------------------------------------------------------------------------
// plumbing
// ---------------------------------------------------------------------------

function send(res, status, payload) {
  const body = JSON.stringify(payload ?? {});
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error('body too large'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(Object.assign(new Error('body is not valid JSON'), { status: 400 }));
      }
    });
    req.on('error', reject);
  });
}

function authorized(req) {
  const header = req.headers.authorization ?? '';
  const m = header.match(/^Bearer\s+(.+)$/i);
  if (!m) return false;
  const given = Buffer.from(m[1]);
  const want = Buffer.from(config.apiToken);
  if (given.length !== want.length) return false;
  return timingSafeEqual(given, want);
}

// ---------------------------------------------------------------------------
// handlers
// ---------------------------------------------------------------------------

/**
 * POST /v1/messages  { to, body, sender_slug? }
 *
 * Resolves the contact honouring sticky routing, resolves the conversation, runs
 * can_send(), and enqueues. Returns 202 (accepted for sending) - never 200,
 * because nothing has been sent yet and this API deliberately refuses to imply
 * otherwise.
 */
async function postMessages(req, res, payload) {
  const to = payload?.to;
  const body = payload?.body;
  const senderSlug = payload?.sender_slug;

  if (typeof to !== 'string' || !to.trim()) return send(res, 400, { error: 'to is required' });
  if (typeof body !== 'string' || !body.trim()) return send(res, 400, { error: 'body is required' });

  const normalized = dbapi.normalizeAddress(to);
  if (!normalized) return send(res, 400, { error: 'to does not normalize to an address', to });

  // Which sender would a NEW contact be pinned to? Existing contacts ignore this
  // entirely - sticky routing is absolute.
  let defaultSender;
  if (senderSlug) {
    defaultSender = await dbapi.getSenderBySlug(senderSlug);
    if (!defaultSender) return send(res, 400, { error: 'unknown sender_slug', sender_slug: senderSlug });
  } else {
    const active = await dbapi.getSenders({ onlyActive: true });
    defaultSender = active[0];
    if (!defaultSender) return send(res, 503, { error: 'no active sender available' });
  }

  const { contact, created, stickyMismatch } = await dbapi.resolveContact(
    to,
    defaultSender.id,
    payload?.display_name,
  );

  if (stickyMismatch) {
    // The caller named a sender this contact is not pinned to. Refuse. Do NOT
    // reroute, and do NOT quietly honour the sticky sender either - the caller
    // asked for something impossible and needs to know.
    const sticky = (await dbapi.getSenders()).find((s) => s.id === contact.sticky_sender_id);
    return send(res, 409, {
      error: 'sticky_sender_mismatch',
      detail:
        'This contact is permanently pinned to another sender. Rerouting is never performed: ' +
        'the recipient would see the conversation continue from a different identity.',
      requested_sender: senderSlug ?? defaultSender.slug,
      sticky_sender: sticky?.slug ?? contact.sticky_sender_id,
    });
  }

  const { conversation } = await dbapi.resolveConversation(contact);

  // THE GATE.
  const verdict = await dbapi.canSend(contact.id);
  if (!verdict?.allowed) {
    const reason = verdict?.reason ?? 'refused';
    // 409 for a transient/ordering refusal, 403 for a policy refusal.
    const status = reason === 'in_flight_message_exists' ? 409 : 403;
    return send(res, status, {
      error: 'refused',
      reason,
      contact_id: contact.id,
      conversation_id: conversation.id,
    });
  }

  const message = await dbapi.enqueueOutbound({
    conversation,
    senderId: contact.sticky_sender_id,
    body,
  });

  log.info('enqueued', {
    messageId: message.id,
    contactId: contact.id,
    sender: defaultSender.slug,
    contactCreated: created,
  });

  return send(res, 202, {
    id: message.id,
    state: message.state,
    conversation_id: conversation.id,
    contact_id: contact.id,
    sender: defaultSender.slug,
    temp_guid: message.temp_guid,
    queued_at: message.queued_at,
  });
}

async function getMessageById(req, res, id) {
  const message = await dbapi.getMessage(id);
  if (!message) return send(res, 404, { error: 'message not found', id });
  const attempts = await dbapi.getAttempts(id);
  return send(res, 200, {
    id: message.id,
    conversation_id: message.conversation_id,
    direction: message.direction,
    state: message.state,
    body: message.body,
    provider_guid: message.provider_guid,
    temp_guid: message.temp_guid,
    error_code: message.error_code,
    // reconciled=false on a failed row means the outcome is genuinely UNKNOWN,
    // not that the message failed. See docs/BRIDGE-SCHEMA.md §5.
    reconciled: message.reconciled,
    queued_at: message.queued_at,
    sending_at: message.sending_at,
    sent_at: message.sent_at,
    delivered_at: message.delivered_at,
    failed_at: message.failed_at,
    attempts: attempts.map((a) => ({
      attempt_no: a.attempt_no,
      outcome: a.outcome,
      http_status: a.http_status,
      created_at: a.created_at,
    })),
  });
}

async function getContact(req, res, address) {
  const normalized = dbapi.normalizeAddress(address);
  if (!normalized) return send(res, 400, { error: 'address does not normalize', address });

  const contact = await dbapi.getContactByAddress(normalized);
  const suppression = await dbapi.getSuppression(normalized);

  if (!contact) {
    // A suppression can exist without a contact - someone can opt out before we
    // ever created a row for them. Report that truthfully.
    return send(res, 404, {
      error: 'contact not found',
      normalized_address: normalized,
      suppression: suppression
        ? { reason: suppression.reason, created_at: suppression.created_at }
        : null,
    });
  }

  const consent = await dbapi.latestConsent(contact.id);
  const senders = await dbapi.getSenders();
  const sticky = senders.find((s) => s.id === contact.sticky_sender_id);
  const verdict = await dbapi.canSend(contact.id);

  return send(res, 200, {
    contact: {
      id: contact.id,
      normalized_address: contact.normalized_address,
      raw_address: contact.raw_address,
      display_name: contact.display_name,
      created_at: contact.created_at,
    },
    sticky_sender: sticky
      ? { slug: sticky.slug, apple_email: sticky.apple_email, status: sticky.status, paused_reason: sticky.paused_reason }
      : { id: contact.sticky_sender_id, missing: true },
    consent: consent
      ? { status: consent.status, source: consent.source, granted_at: consent.granted_at, revoked_at: consent.revoked_at }
      : null,
    suppression: suppression
      ? { reason: suppression.reason, created_at: suppression.created_at }
      : null,
    can_send: verdict,
  });
}

async function postSuppressions(req, res, payload) {
  const address = payload?.address ?? payload?.to;
  const reason = payload?.reason ?? 'manual';
  if (typeof address !== 'string' || !address.trim()) {
    return send(res, 400, { error: 'address is required' });
  }
  if (!['stop', 'dnc', 'bounce', 'manual'].includes(reason)) {
    return send(res, 400, { error: 'reason must be one of stop | dnc | bounce | manual', reason });
  }
  const { suppression, created } = await dbapi.addSuppression(address, reason);
  log.info('suppression recorded', { normalized: suppression?.normalized_address, reason, created });
  // Suppression is global across every sender and every conversation, by design.
  return send(res, created ? 201 : 200, {
    normalized_address: suppression?.normalized_address,
    reason: suppression?.reason,
    created,
    scope: 'global',
  });
}

/**
 * POST /v1/consents  { address, status, source, evidence? }
 *
 * Not in the original endpoint list, but can_send() refuses every contact with
 * `no_consent_on_record`, so without a way to record consent the gateway can
 * never send anything. The consent ledger is append-only: a revocation is a new
 * row, never an update.
 */
async function postConsents(req, res, payload) {
  const address = payload?.address;
  const status = payload?.status ?? 'granted';
  const source = payload?.source;
  if (typeof address !== 'string' || !address.trim()) return send(res, 400, { error: 'address is required' });
  if (!['granted', 'revoked'].includes(status)) return send(res, 400, { error: 'status must be granted | revoked' });
  if (typeof source !== 'string' || !source.trim()) {
    return send(res, 400, { error: 'source is required (web_form, import, verbal, reply_optin, operator, ...)' });
  }

  const contact = await dbapi.getContactByAddress(address);
  if (!contact) return send(res, 404, { error: 'contact not found - enqueue a message or create the contact first', address });

  const row = await dbapi.recordConsent({
    contactId: contact.id,
    status,
    source,
    evidence: payload?.evidence,
  });
  return send(res, 201, { id: row.id, contact_id: contact.id, status: row.status, source: row.source });
}

/**
 * POST /v1/webhooks/bluebubbles
 *
 * BlueBubbles posts { type, data }. For message events `data` carries
 * { guid, text, dateCreated, isFromMe, handle: { address }, chats: [{ guid }], error }.
 *
 * Defect B: the SAME event arrives twice, ~1ms apart, with an IDENTICAL guid -
 * measured on 8 of 11 outbound sends. messages_provider_guid_uidx raises 23505 on
 * the second copy and we report `deduped`. That is the normal path, not an error.
 */
async function postWebhook(req, res, payload) {
  const type = payload?.type ?? null;
  const d = payload?.data ?? null;

  if (!type || !d || typeof d !== 'object') return send(res, 200, { ok: true, ignored: 'no event data' });
  if (!['new-message', 'updated-message'].includes(type)) {
    return send(res, 200, { ok: true, ignored: type });
  }

  const providerGuid = d.guid ?? null;
  const text = typeof d.text === 'string' ? d.text : null;
  const isFromMe = d.isFromMe === true;
  const chatGuid = (Array.isArray(d.chats) && d.chats[0]?.guid) || d.chatGuid || null;
  const address = d.handle?.address ?? (typeof d.handle === 'string' ? d.handle : null);
  const deliveredAt = d.dateDelivered ? new Date(Number(d.dateDelivered)).toISOString() : null;

  if (!providerGuid) return send(res, 200, { ok: true, ignored: 'no provider guid' });

  // Already seen? Cheap dedupe before we do anything else. The unique index is
  // still the real guarantee - this just avoids the wasted work.
  const existing = await dbapi.findMessageByProviderGuid(providerGuid);
  if (existing) {
    // Enrich an outbound row with the delivery confirmation, if this is that.
    if (deliveredAt && !existing.delivered_at) {
      await dbapi.db.from('messages').update({ delivered_at: deliveredAt }).eq('id', existing.id);
    }
    log.info('webhook deduped by provider_guid', { providerGuid, type, messageId: existing.id });
    return send(res, 200, { ok: true, deduped: true, message_id: existing.id });
  }

  // ---- outbound echo -------------------------------------------------------
  if (isFromMe) {
    // Match the echo to the queued row by text within its conversation. tempGuid
    // is present on the webhook only for some event shapes, so try it first.
    const byTemp = d.tempGuid ? await dbapi.findMessageByTempGuid(d.tempGuid) : null;
    let target = byTemp;

    if (!target && chatGuid) {
      const conv = await dbapi.findConversationByChatGuid(chatGuid);
      if (conv) {
        const { data } = await dbapi.db
          .from('messages')
          .select('*')
          .eq('conversation_id', conv.id)
          .eq('direction', 'outbound')
          .is('provider_guid', null)
          .eq('body', text ?? '')
          .order('queued_at', { ascending: false })
          .limit(1);
        target = data?.[0] ?? null;
      }
    }

    if (!target) {
      log.info('outbound echo with no matching row (native send, or pre-bridge traffic)', {
        providerGuid, chatGuid,
      });
      return send(res, 200, { ok: true, unmatched: true });
    }

    const { deduped } = await dbapi.attachProviderGuid(target.id, providerGuid, {
      ...(deliveredAt ? { delivered_at: deliveredAt } : {}),
    });
    log.info('outbound echo ingested', { providerGuid, messageId: target.id, deduped });
    return send(res, 200, { ok: true, deduped, message_id: target.id, direction: 'outbound' });
  }

  // ---- inbound reply -------------------------------------------------------
  if (!address) return send(res, 200, { ok: true, ignored: 'inbound with no handle address' });

  const contact = await dbapi.getContactByAddress(address);
  if (!contact) {
    // Someone we have no row for. Still honour a STOP - the suppression list is
    // keyed on the address, not on a contact, precisely so this works.
    if (isStopMessage(text)) {
      await dbapi.addSuppression(address, 'stop');
      log.warn('STOP from an unknown address - suppressed anyway', { address });
      return send(res, 200, { ok: true, suppressed: true, unknown_contact: true });
    }
    log.info('inbound from unknown address - not recorded', { address });
    return send(res, 200, { ok: true, ignored: 'unknown contact' });
  }

  const { conversation } = await dbapi.resolveConversation(contact);

  const { deduped } = await dbapi.ingestInbound({
    conversationId: conversation.id,
    senderId: contact.sticky_sender_id,
    providerGuid,
    body: text,
    deliveredAt,
  });

  if (chatGuid && !conversation.provider_chat_guid) {
    try {
      await dbapi.setConversationChatGuid(conversation.id, chatGuid);
    } catch (err) {
      log.warn('could not attach chat guid from inbound webhook', { chatGuid, err });
    }
  }

  let suppressed = false;
  if (isStopMessage(text)) {
    const { created } = await dbapi.addSuppression(contact.normalized_address, 'stop');
    // The ledger and the suppression list are separate records of separate
    // facts: the suppression is what can_send() enforces, the consent row is the
    // audit trail of the person's decision.
    await dbapi.recordConsent({
      contactId: contact.id,
      status: 'revoked',
      source: 'reply_optin',
      evidence: { via: 'inbound_stop', provider_guid: providerGuid, text },
    });
    suppressed = true;
    log.warn('STOP received - contact suppressed globally', {
      address: contact.normalized_address, created, providerGuid,
    });
  }

  log.info('inbound ingested', { providerGuid, contactId: contact.id, deduped, suppressed });
  return send(res, 200, { ok: true, deduped, direction: 'inbound', suppressed });
}

async function getHealth(req, res) {
  const senders = await dbapi.getSenders();
  const depth = await dbapi.queueDepth();
  const allActive = senders.length > 0 && senders.every((s) => s.status === 'active');
  return send(res, 200, {
    ok: allActive,
    senders: senders.map((s) => ({
      slug: s.slug,
      apple_email: s.apple_email,
      status: s.status,
      paused_reason: s.paused_reason,
      url: s.bluebubbles_url,
      port: s.bluebubbles_port,
    })),
    queue: depth,
    // Non-zero unreconciled_failed is the Defect A triage queue. It must be
    // drained before any retry sweep is considered safe.
    note: depth.unreconciled_failed > 0
      ? `${depth.unreconciled_failed} failed message(s) have NOT been reconciled - outcome unknown, do not retry`
      : undefined,
  });
}

// ---------------------------------------------------------------------------
// router
// ---------------------------------------------------------------------------

export async function route(req, res) {
  const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
  const p = url.pathname.replace(/\/+$/, '') || '/';
  const method = req.method;

  // The webhook is the one unauthenticated route: BlueBubbles cannot be told to
  // send an Authorization header. It is loopback-only.
  const isWebhook = method === 'POST' && p === '/v1/webhooks/bluebubbles';

  if (!isWebhook) {
    if (!config.apiToken) {
      return send(res, 500, { error: 'BRIDGE_API_TOKEN is not configured - refusing to serve unauthenticated' });
    }
    if (!authorized(req)) {
      res.setHeader('WWW-Authenticate', 'Bearer');
      return send(res, 401, { error: 'unauthorized' });
    }
  }

  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readBody(req) : {};

  if (method === 'POST' && p === '/v1/messages') return postMessages(req, res, body);
  if (method === 'GET' && p.startsWith('/v1/messages/')) {
    return getMessageById(req, res, decodeURIComponent(p.slice('/v1/messages/'.length)));
  }
  if (method === 'GET' && p.startsWith('/v1/contacts/')) {
    return getContact(req, res, decodeURIComponent(p.slice('/v1/contacts/'.length)));
  }
  if (method === 'POST' && p === '/v1/suppressions') return postSuppressions(req, res, body);
  if (method === 'POST' && p === '/v1/consents') return postConsents(req, res, body);
  if (isWebhook) return postWebhook(req, res, body);
  if (method === 'GET' && p === '/v1/health') return getHealth(req, res);

  return send(res, 404, { error: 'not found', path: p });
}

export function createServer() {
  return http.createServer((req, res) => {
    const started = Date.now();
    route(req, res)
      .catch((err) => {
        log.error('unhandled request error', { path: req.url, method: req.method, err });
        if (!res.headersSent) send(res, err?.status ?? 500, { error: err?.message ?? 'internal error' });
      })
      .finally(() => {
        log.debug('request', {
          method: req.method,
          path: req.url?.split('?')[0],
          status: res.statusCode,
          ms: Date.now() - started,
        });
      });
  });
}

const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const server = createServer();
  server.listen(config.apiPort, config.apiHost, () => {
    log.info('gateway listening', {
      host: config.apiHost,
      port: config.apiPort,
      authenticated: Boolean(config.apiToken),
    });
  });
  for (const sig of ['SIGTERM', 'SIGINT']) {
    process.on(sig, () => {
      log.info('shutting down', { sig });
      server.close(() => process.exit(0));
      setTimeout(() => process.exit(1), 10_000).unref();
    });
  }
}
