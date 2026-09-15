#!/usr/bin/env node
// worker.mjs - the send loop.
//
// The whole design is a response to one measured fact (docs/PHASE1-REPORT.md):
// the BlueBubbles HTTP response carries no reliable information about whether a
// message was delivered. 500s and timeouts accompany successful sends; 200s
// precede delivery by minutes when the Mac is offline.
//
// Therefore:
//   * The database, not the API response, decides state.
//   * Every ambiguous outcome goes to `failed` with reconciled = false and STOPS.
//     The state-machine trigger physically refuses to let it be retried until a
//     reconcile pass has consulted provider state.
//   * Two send paths, not one: chat/new exactly once per conversation (75-120s,
//     always times out, delivers anyway), message/text (0.76s, clean 200) forever
//     after. Treating chat/new as the normal path would cap throughput at one
//     message per two minutes and produce a timeout on literally every send.
//
// Pacing is deliberately slow. The constraint is Apple's tolerance for a young
// sender identity, not the machine.

import config from '../lib/env.mjs';
import { createLogger } from '../lib/log.mjs';
import { BlueBubblesClient } from '../lib/bluebubbles.mjs';
import * as dbapi from '../lib/db.mjs';
import { classifyRefusal, retryAfterOf } from '../lib/caps.mjs';

const log = createLogger('worker');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitteredInterval = () => {
  const { workerMinIntervalMs: lo, workerMaxIntervalMs: hi } = config;
  return Math.round(lo + Math.random() * Math.max(0, hi - lo));
};

let shuttingDown = false;
let inFlight = null; // promise for the send currently in progress

// ---------------------------------------------------------------------------
// Reconciliation - run after every ambiguous outcome, before any retry
// ---------------------------------------------------------------------------

/**
 * Decide, from provider state, whether an ambiguous send actually landed, and
 * move the message accordingly.
 *
 * Three outcomes, and the third one matters as much as the other two:
 *   found          -> failed -> sent, reconciled = true, provider_guid attached.
 *   not found      -> reconciled = true only. The row stays `failed`, and is now
 *                     legally requeueable. Requeueing is left to an operator /
 *                     retry sweep rather than done inline, so a genuinely broken
 *                     address cannot spin.
 *   could not read -> reconciled stays FALSE. "We could not check" is not
 *                     "it did not deliver". The row lands in
 *                     messages_unreconciled_failed_idx, which is the triage queue
 *                     that must be empty before any retry sweep is safe.
 */
async function reconcile(bb, message, { address, chatGuid, sinceMs }) {
  await sleep(config.reconcileSettleMs);

  let result;
  try {
    result = await bb.reconcileByTempGuid(message.temp_guid, message.body, {
      address,
      chatGuid,
      sinceMs,
    });
  } catch (err) {
    log.error('reconcile could not read provider state - leaving reconciled=false', {
      messageId: message.id,
      tempGuid: message.temp_guid,
      err,
    });
    return { resolved: false, reason: 'provider_state_unreadable' };
  }

  if (!result.found) {
    await dbapi.markReconciledNotDelivered(message.id);
    log.warn('reconciled: message did NOT deliver, safe to requeue', {
      messageId: message.id,
      tempGuid: message.temp_guid,
      evidence: result.evidence,
    });
    return { resolved: true, delivered: false };
  }

  await dbapi.recordAttempt(message.id, {
    httpStatus: null,
    response: {
      reconcile: true,
      confidence: result.confidence,
      providerGuid: result.providerGuid,
      providerError: result.providerError,
      evidence: result.evidence,
    },
    outcome: 'reconciled_sent',
  });

  await dbapi.reconcileToSent(message.id, {
    providerGuid: result.providerGuid,
    deliveredAt: result.deliveredAt,
  });

  if (result.chatGuid) {
    await attachChatGuid(message.conversation_id, result.chatGuid);
  }

  log.info('reconciled: message DID deliver (Defect A false failure)', {
    messageId: message.id,
    providerGuid: result.providerGuid,
    confidence: result.confidence,
  });
  return { resolved: true, delivered: true, providerGuid: result.providerGuid };
}

async function attachChatGuid(conversationId, chatGuid) {
  try {
    await dbapi.setConversationChatGuid(conversationId, chatGuid);
    log.info('conversation chat guid recorded', { conversationId, chatGuid });
  } catch (err) {
    log.warn('could not attach chat guid', { conversationId, chatGuid, err });
  }
}

// ---------------------------------------------------------------------------
// Unreachable addresses - pay the 90s cost once, never again
// ---------------------------------------------------------------------------

/**
 * error=22 means the address is not registered with Apple. That send burned
 * 90-100 seconds. Record it as a bounce suppression so the queue never stalls on
 * this address a second time - suppression is global and is can_send()'s first
 * check, so every future message to it is refused at enqueue time in microseconds.
 */
async function suppressUnreachable(address, messageId) {
  const { created } = await dbapi.addSuppression(address, 'bounce');
  log.warn('address unreachable (error=22) - added bounce suppression', {
    address,
    messageId,
    created,
  });
}

// ---------------------------------------------------------------------------
// The two send paths
// ---------------------------------------------------------------------------

async function sendViaExistingChat(bb, message, { chatGuid, address }) {
  const sinceMs = Date.now();
  const res = await bb.sendText({
    chatGuid,
    tempGuid: message.temp_guid,
    message: message.body,
  });

  log.info('message/text returned', {
    messageId: message.id,
    classification: res.classification,
    httpStatus: res.httpStatus,
    elapsedMs: res.elapsedMs,
    providerGuid: res.providerGuid,
    providerError: res.providerError,
  });

  if (res.classification === 'ok' && (res.providerError === 0 || res.providerError === null)) {
    await dbapi.recordAttempt(message.id, {
      httpStatus: res.httpStatus,
      response: res.body,
      outcome: 'accepted',
    });
    // 'accepted' is NOT 'delivered'. sent_at records acceptance; delivered_at is
    // filled in later by the echo webhook, which may lag by minutes.
    await dbapi.markSent(message.id, { providerGuid: res.providerGuid });
    return { state: 'sent', providerGuid: res.providerGuid };
  }

  if (res.classification === 'ok' && res.providerError === 22) {
    await dbapi.recordAttempt(message.id, {
      httpStatus: res.httpStatus,
      response: res.body,
      outcome: 'error',
    });
    // An explicit provider verdict. Not ambiguous, so it is reconciled by
    // definition - there is nothing to check against.
    await dbapi.markFailed(message.id, { errorCode: 'imessage:22', reconciled: true });
    await suppressUnreachable(address, message.id);
    return { state: 'failed', reason: 'unreachable' };
  }

  // Everything else is ambiguous: timeout, 5xx, transport error, or a non-zero
  // provider error we do not have a rule for.
  await dbapi.recordAttempt(message.id, {
    httpStatus: res.httpStatus,
    response: res.body,
    outcome: res.classification === 'timeout' ? 'timeout' : 'error',
  });
  await dbapi.markFailed(message.id, {
    errorCode: res.classification === 'timeout' ? 'timeout' : `http:${res.httpStatus ?? 'transport'}`,
    reconciled: false,
  });

  return {
    state: 'failed',
    reason: 'ambiguous',
    reconcile: await reconcile(bb, message, { address, chatGuid, sinceMs }),
  };
}

async function sendViaNewChat(bb, message, { address }) {
  const sinceMs = Date.now();
  log.info('no provider_chat_guid - taking the chat/new path (expect a 75-120s timeout)', {
    messageId: message.id,
    address,
  });

  const res = await bb.createChat({
    address,
    tempGuid: message.temp_guid,
    message: message.body,
  });

  log.info('chat/new returned', {
    messageId: message.id,
    classification: res.classification,
    httpStatus: res.httpStatus,
    elapsedMs: res.elapsedMs,
    chatGuid: res.chatGuid,
  });

  // The rare clean case. Measured never to happen, handled anyway.
  if (res.classification === 'ok' && res.chatGuid) {
    await dbapi.recordAttempt(message.id, {
      httpStatus: res.httpStatus,
      response: res.body,
      outcome: 'accepted',
    });
    await attachChatGuid(message.conversation_id, res.chatGuid);
    await dbapi.markSent(message.id, { providerGuid: res.providerGuid });
    return { state: 'sent', providerGuid: res.providerGuid, chatGuid: res.chatGuid };
  }

  // The measured case: timeout (or -1728 "Can't get chat id" behind a 500) while
  // the chat was created and the message delivered. This is EXPECTED, and the
  // reconcile pass is how the chat GUID gets recovered.
  await dbapi.recordAttempt(message.id, {
    httpStatus: res.httpStatus,
    response: res.body,
    outcome: res.classification === 'timeout' ? 'timeout' : 'error',
  });
  await dbapi.markFailed(message.id, {
    errorCode: res.classification === 'timeout' ? 'chatnew:timeout' : `chatnew:http:${res.httpStatus ?? 'transport'}`,
    reconciled: false,
  });

  return {
    state: 'failed',
    reason: 'chat_new_ambiguous',
    reconcile: await reconcile(bb, message, { address, chatGuid: res.chatGuid, sinceMs }),
  };
}

// ---------------------------------------------------------------------------
// Process one message
// ---------------------------------------------------------------------------

async function processMessage(bb, sender, row) {
  const conversation = row.conversations;

  // 1. THE GATE. can_send_message() delegates suppression / consent / sender
  //    status / sticky-sender to can_send() unchanged, and additionally requires
  //    this message to be the FIFO head. Its verdict is final.
  const verdict = await dbapi.canSendMessage(row.id);
  if (!verdict?.allowed) {
    const reason = verdict?.reason;
    const kind = classifyRefusal(reason);
    const retryAfter = retryAfterOf(verdict);

    log.info('gate refused', { messageId: row.id, reason, retryAfter, ...kind });

    // A refusal that can never clear on its own is a terminal failure; leaving it
    // queued would block the conversation forever. Suppression and revoked
    // consent are exactly that.
    //
    // A CAP refusal is the opposite and must never fail the message: operating
    // caps and quiet hours [guide step 36] say "not yet", not "no". The row
    // stays `queued` and goes out unchanged once the window reopens - which is
    // why `retry_after` is logged rather than acted on. Everything else (paused
    // sender, in-flight, not-yet-effective consent) is transient in the same
    // way and also stays queued.
    if (kind.terminal) {
      await dbapi.markFailed(row.id, { errorCode: `refused:${reason}`, reconciled: true });
    }
    return { skipped: true, reason, retryAfter, capped: kind.cap, senderWide: kind.senderWide };
  }

  // 2. Claim it. queued -> sending. Losing this race is routine.
  let claimed;
  try {
    claimed = await dbapi.claimMessage(row.id);
  } catch (err) {
    if (dbapi.isUniqueViolation(err)) {
      log.info('claim lost to messages_one_sending_per_conversation_uidx', { messageId: row.id });
      return { skipped: true, reason: 'claim_lost' };
    }
    throw err;
  }
  if (!claimed) {
    log.info('claim lost (state was no longer queued)', { messageId: row.id });
    return { skipped: true, reason: 'claim_lost' };
  }

  // 3. Resolve the recipient address.
  const { data: contact, error } = await dbapi.db
    .from('contacts')
    .select('normalized_address')
    .eq('id', conversation.contact_id)
    .maybeSingle();
  dbapi.throwIf(error, 'processMessage.contact');
  const address = contact?.normalized_address;
  if (!address) {
    await dbapi.markFailed(claimed.id, { errorCode: 'no_address', reconciled: true });
    return { skipped: true, reason: 'no_address' };
  }

  // 4. Pick the path.
  const result = conversation.provider_chat_guid
    ? await sendViaExistingChat(bb, claimed, { chatGuid: conversation.provider_chat_guid, address })
    : await sendViaNewChat(bb, claimed, { address });

  log.info('send complete', { messageId: claimed.id, sender: sender.slug, ...result });
  return result;
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

async function tick() {
  // Only ACTIVE senders. A paused sender is skipped and never routed around:
  // its contacts simply do not receive messages until it is fixed (guide step 32).
  const senders = await dbapi.getSenders({ onlyActive: true });
  if (senders.length === 0) {
    log.debug('no active senders');
    return false;
  }

  let didWork = false;

  for (const sender of senders) {
    if (shuttingDown) break;

    const bb = BlueBubblesClient.forSender(sender);
    const candidates = await dbapi.claimableMessages(sender.id, 25);
    if (candidates.length === 0) continue;

    // One message per conversation per tick. The database enforces this too,
    // but not queueing the work at all is cheaper than losing the race.
    const seen = new Set();
    for (const row of candidates) {
      if (shuttingDown) break;
      if (seen.has(row.conversation_id)) continue;
      seen.add(row.conversation_id);

      inFlight = processMessage(bb, sender, row).catch((err) => {
        log.error('processMessage threw', { messageId: row.id, err });
        return { error: true };
      });
      const r = await inFlight;
      inFlight = null;

      // A sender-wide cap (daily, hourly, quiet hours) refuses every message this
      // sender has, so walking the rest of its queue would be 24 more pointless
      // gate calls per tick. Stop here and move to the next sender; the messages
      // stay queued and the next tick re-asks.
      //
      // cap_new_conversation is NOT sender-wide and deliberately does not break:
      // it refuses only first contacts, so replies into existing threads behind
      // it must keep flowing.
      if (r?.senderWide) {
        log.info('sender-wide cap reached - skipping the rest of this sender this tick', {
          sender: sender.slug,
          reason: r.reason,
          retryAfter: r.retryAfter,
        });
        break;
      }

      if (!r?.skipped) {
        didWork = true;
        // Rate limit: per sender, jittered. Deliberately slow.
        const wait = jitteredInterval();
        log.debug('pacing', { sender: sender.slug, waitMs: wait });
        await sleep(wait);
      }
    }
  }

  return didWork;
}

async function main() {
  log.info('worker starting', {
    supabase: config.supabaseUrl,
    pacingMs: [config.workerMinIntervalMs, config.workerMaxIntervalMs],
    chatNewTimeoutMs: config.bbTimeoutChatNewMs,
  });

  while (!shuttingDown) {
    let didWork = false;
    try {
      didWork = await tick();
    } catch (err) {
      log.error('tick failed', { err });
    }
    if (!didWork && !shuttingDown) await sleep(config.workerIdlePollMs);
  }

  // Graceful shutdown: let the send in progress finish, so we never abandon a
  // message in `sending` with an unknown provider outcome.
  if (inFlight) {
    log.info('waiting for in-flight send to settle before exit');
    try {
      await inFlight;
    } catch { /* already logged */ }
  }
  log.info('worker stopped');
}

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    if (shuttingDown) {
      log.warn('second signal - exiting immediately', { sig });
      process.exit(1);
    }
    log.info('shutdown requested, finishing current send', { sig });
    shuttingDown = true;
  });
}

const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  main().then(
    () => process.exit(0),
    (err) => {
      log.error('worker crashed', { err });
      process.exit(1);
    },
  );
}

export { tick, processMessage, reconcile };
