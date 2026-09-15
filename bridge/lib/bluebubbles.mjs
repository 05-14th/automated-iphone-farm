// bluebubbles.mjs - typed wrapper over the BlueBubbles Server REST API v1.
//
// Routes used (all authenticated with ?password=, per authMiddleware):
//   GET  /api/v1/ping
//   GET  /api/v1/server/info
//   GET  /api/v1/handle/:address
//   POST /api/v1/chat/query
//   GET  /api/v1/chat/:guid/message
//   POST /api/v1/chat/new
//   POST /api/v1/message/text
//
// THE ONE THING TO UNDERSTAND ABOUT THIS FILE (docs/PHASE1-REPORT.md):
//
//   The HTTP response carries no reliable information about delivery, in either
//   direction. A 500 or a timeout routinely accompanies a message that WAS
//   delivered; a 200 can precede delivery by minutes if the Mac was offline.
//
// So every call here returns a *classification* - ok | timeout | http_error |
// transport_error - and never an opinion about whether the message landed. Only
// reconcileByTempGuid() is allowed to have that opinion, and it forms it by
// reading provider state.

import config from './env.mjs';
import { createLogger } from './log.mjs';

const log = createLogger('bluebubbles');

export class BlueBubblesClient {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl  e.g. http://127.0.0.1:12341
   * @param {string} opts.password server password
   * @param {string} [opts.slug]   sender slug, for log correlation
   */
  constructor({ baseUrl, password, slug = 'sender01', timeouts = {} } = {}) {
    this.baseUrl = (baseUrl || config.bbUrl).replace(/\/+$/, '');
    this.password = password || config.bbPassword;
    this.slug = slug;
    this.timeouts = {
      message: timeouts.message ?? config.bbTimeoutMessageMs,
      chatNew: timeouts.chatNew ?? config.bbTimeoutChatNewMs,
      query: timeouts.query ?? config.bbTimeoutQueryMs,
    };
  }

  static forSender(sender) {
    const port = sender.bluebubbles_port;
    const base = /:\d+$/.test(sender.bluebubbles_url)
      ? sender.bluebubbles_url
      : `${sender.bluebubbles_url.replace(/\/+$/, '')}:${port}`;
    return new BlueBubblesClient({ baseUrl: base, slug: sender.slug });
  }

  #url(path) {
    const sep = path.includes('?') ? '&' : '?';
    return `${this.baseUrl}/api/v1/${path}${sep}password=${encodeURIComponent(this.password)}`;
  }

  /**
   * One HTTP call. Returns { classification, httpStatus, body, elapsedMs } and
   * NEVER throws for a timeout or a 5xx - those are expected, meaningful states.
   * It throws only for a programming error.
   */
  async #call(method, path, { body, timeoutMs } = {}) {
    const started = Date.now();
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs ?? this.timeouts.query);
    try {
      const res = await fetch(this.#url(path), {
        method,
        signal: ac.signal,
        headers: body
          ? { 'Content-Type': 'application/json', Accept: 'application/json' }
          : { Accept: 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
      const text = await res.text();
      let parsed = null;
      try {
        parsed = text ? JSON.parse(text) : null;
      } catch {
        parsed = { raw: text.slice(0, 2000) };
      }
      const elapsedMs = Date.now() - started;
      const classification = res.ok ? 'ok' : 'http_error';
      return { classification, httpStatus: res.status, body: parsed, elapsedMs };
    } catch (err) {
      const elapsedMs = Date.now() - started;
      const aborted = err?.name === 'AbortError';
      return {
        classification: aborted ? 'timeout' : 'transport_error',
        httpStatus: null,
        body: { error: { name: err?.name, message: String(err?.message ?? err) } },
        elapsedMs,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  // -------------------------------------------------------------------------
  // Read paths
  // -------------------------------------------------------------------------

  async ping() {
    const r = await this.#call('GET', 'ping', { timeoutMs: config.healthTimeoutMs });
    return { ...r, alive: r.classification === 'ok' && r.body?.data === 'pong' };
  }

  async serverInfo() {
    return this.#call('GET', 'server/info', { timeoutMs: config.healthTimeoutMs });
  }

  /**
   * Advisory reachability hint ONLY.
   *
   * GET /handle/:address reads the LOCAL chat.db handle table, so a 200 means
   * "Messages on this Mac has seen this address before", and a 404 means only
   * "never contacted from here" - not "unreachable". There is no true
   * reachability oracle without the Private API.
   *
   * Real unreachability is learned the expensive way: a send to an address that
   * is not registered with Apple hangs 90-100s and comes back error=22. The
   * worker turns that single observation into a suppression (reason 'bounce'),
   * so the queue pays the 90s cost once per address and never again.
   */
  async handleKnown(address) {
    const r = await this.#call('GET', `handle/${encodeURIComponent(address)}`, {
      timeoutMs: this.timeouts.query,
    });
    if (r.classification === 'ok') {
      return { known: true, service: r.body?.data?.service ?? null, raw: r };
    }
    if (r.httpStatus === 404) return { known: false, service: null, raw: r };
    return { known: null, service: null, raw: r }; // unknown - do not act on it
  }

  async chatQuery({ limit = 1000, offset = 0, withRels = ['participants'] } = {}) {
    return this.#call('POST', 'chat/query', {
      body: { limit, offset, with: withRels },
      timeoutMs: this.timeouts.query,
    });
  }

  /** Find the 1:1 chat GUID for an address, preferring iMessage over SMS chats. */
  async findChatGuid(address) {
    const target = String(address).trim().toLowerCase();
    const r = await this.chatQuery({ limit: 1000, withRels: ['participants'] });
    if (r.classification !== 'ok') return { guid: null, raw: r };
    const chats = Array.isArray(r.body?.data) ? r.body.data : [];
    const matches = chats.filter(
      (c) =>
        Array.isArray(c.participants) &&
        c.participants.length === 1 &&
        String(c.participants[0]?.address ?? '').toLowerCase() === target,
    );
    matches.sort((a, b) => {
      const ai = String(a.guid).toLowerCase().startsWith('imessage') ? 0 : 1;
      const bi = String(b.guid).toLowerCase().startsWith('imessage') ? 0 : 1;
      return ai - bi;
    });
    return { guid: matches[0]?.guid ?? null, raw: r };
  }

  async chatMessages(chatGuid, { limit = 50, offset = 0, sort = 'DESC' } = {}) {
    const path = `chat/${encodeURIComponent(chatGuid)}/message?limit=${limit}&offset=${offset}&sort=${sort}`;
    return this.#call('GET', path, { timeoutMs: this.timeouts.query });
  }

  async messageQuery({ limit = 50, offset = 0, withRels = ['chats'], sort = 'DESC' } = {}) {
    return this.#call('POST', 'message/query', {
      body: { limit, offset, with: withRels, sort },
      timeoutMs: this.timeouts.query,
    });
  }

  // -------------------------------------------------------------------------
  // Write paths - the two send strategies
  // -------------------------------------------------------------------------

  /**
   * FAST PATH. Measured at 0.76s with a clean 200. This is what ~every send
   * after the first one on a conversation uses.
   *
   * Still not proof of delivery (a send issued while offline also returns 200).
   */
  async sendText({ chatGuid, tempGuid, message }) {
    const r = await this.#call('POST', 'message/text', {
      body: { chatGuid, tempGuid, message, method: 'apple-script' },
      timeoutMs: this.timeouts.message,
    });
    return {
      ...r,
      tempGuid,
      providerGuid: r.body?.data?.guid ?? null,
      providerError: r.body?.data?.error ?? null,
    };
  }

  /**
   * SLOW PATH, used EXACTLY ONCE per conversation.
   *
   * Measured: 75-120s and it ALWAYS times out - while delivering the message and
   * creating the chat. It additionally throws AppleScript -1728 "Can't get chat
   * id" after the chat exists, which is why conversations.provider_chat_guid is
   * nullable and why a reconcile pass, not the response, supplies the GUID.
   *
   * The caller must EXPECT classification 'timeout' here and treat it as normal.
   */
  async createChat({ address, tempGuid, message, service = 'iMessage' }) {
    const r = await this.#call('POST', 'chat/new', {
      body: { addresses: [address], message, method: 'apple-script', service, tempGuid },
      timeoutMs: this.timeouts.chatNew,
    });
    return {
      ...r,
      tempGuid,
      chatGuid: r.body?.data?.guid ?? null,
      providerGuid: r.body?.data?.messages?.[0]?.guid ?? null,
    };
  }

  // -------------------------------------------------------------------------
  // Reconciliation - the ONLY place allowed to decide whether a send landed
  // -------------------------------------------------------------------------

  /**
   * Did the ambiguous send identified by `tempGuid` actually deliver?
   *
   * BlueBubbles does not persist tempGuid: it is a client correlation token that
   * the server echoes on the webhook but never writes to chat.db. So provider
   * state cannot be queried by tempGuid directly, and the documented fallback -
   * "text + chat + time window" (docs/BRIDGE-SCHEMA.md §5) - is in fact the only
   * available lookup. The signature keeps tempGuid because it is the identity of
   * the attempt being reconciled and belongs in the log line.
   *
   * This is why the worker sends a message body carrying a unique marker in the
   * e2e path, and why in production `text` must be treated as a weak key: two
   * identical bodies sent to the same chat inside the window are indistinguishable.
   * The window is therefore kept tight and anchored to the attempt's start time.
   *
   * @returns {Promise<{found: boolean, providerGuid: string|null, chatGuid: string|null,
   *                    deliveredAt: string|null, providerError: number|null,
   *                    confidence: 'exact'|'text_window'|'none', evidence: object}>}
   */
  async reconcileByTempGuid(tempGuid, text, { address, chatGuid, sinceMs, windowMs = 10 * 60 * 1000 } = {}) {
    const notFound = (evidence, confidence = 'none') => ({
      found: false,
      providerGuid: null,
      chatGuid: chatGuid ?? null,
      deliveredAt: null,
      providerError: null,
      confidence,
      evidence,
    });

    // 1. Locate the chat. After a chat/new timeout we may not have a GUID yet,
    //    so fall back to a participant scan - this is also how the chat GUID
    //    that chat/new failed to return is recovered.
    let guid = chatGuid ?? null;
    if (!guid && address) {
      const found = await this.findChatGuid(address);
      guid = found.guid;
      if (!guid) {
        return notFound({ step: 'findChatGuid', address, reason: 'no chat for address' });
      }
    }
    if (!guid) return notFound({ step: 'findChatGuid', reason: 'no chatGuid and no address' });

    // 2. Read the tail of that chat and look for our text from us, inside the window.
    const r = await this.chatMessages(guid, { limit: 50, sort: 'DESC' });
    if (r.classification !== 'ok') {
      // We could not READ provider state. That is not "not delivered" - it is
      // "still unknown". The caller must leave reconciled = false.
      const err = new Error(`reconcile could not read provider state: ${r.classification}`);
      err.classification = r.classification;
      err.httpStatus = r.httpStatus;
      throw err;
    }

    const rows = Array.isArray(r.body?.data) ? r.body.data : [];
    const floor = sinceMs ? sinceMs - 60_000 : Date.now() - windowMs;
    const ceiling = (sinceMs ?? Date.now()) + windowMs;

    const hit = rows.find(
      (m) =>
        m?.isFromMe === true &&
        typeof m?.text === 'string' &&
        m.text === text &&
        Number(m.dateCreated) >= floor &&
        Number(m.dateCreated) <= ceiling,
    );

    if (!hit) {
      return notFound({ step: 'chatMessages', chatGuid: guid, scanned: rows.length, floor, ceiling });
    }

    log.info('reconciled from provider state', {
      tempGuid,
      chatGuid: guid,
      providerGuid: hit.guid,
      providerError: hit.error,
    });

    return {
      found: true,
      providerGuid: hit.guid ?? null,
      chatGuid: guid,
      deliveredAt: hit.dateDelivered ? new Date(Number(hit.dateDelivered)).toISOString() : null,
      providerError: typeof hit.error === 'number' ? hit.error : null,
      // 'text_window' not 'exact': tempGuid is not persisted by the provider, so
      // this match is as strong as the uniqueness of the body text. Be honest
      // about that in the audit trail rather than claiming certainty.
      confidence: 'text_window',
      evidence: { chatGuid: guid, scanned: rows.length, dateCreated: hit.dateCreated },
    };
  }
}

export default BlueBubblesClient;
