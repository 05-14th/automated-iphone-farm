#!/usr/bin/env node
// health.mjs - closes the gap flagged in docs/BRIDGE-SCHEMA.md §4.
//
//   "a Mac reboot leaves the sender offline until a human logs into its macOS
//    user [...] That outage is invisible to this schema - the sender row still
//    reads active while its BlueBubbles instance is dead. A health check must
//    flip status; otherwise the worker queues into a void."
//
// This is that health check. It pings each sender's BlueBubbles instance and:
//   * unreachable N times running -> status = 'failed' with a paused_reason
//   * reachable again            -> status = 'active', paused_reason cleared
//
// It only ever un-pauses senders IT paused. A sender an operator paused by hand
// carries a different paused_reason and is left alone - a health checker that
// reactivates a deliberately disabled sender is worse than no health checker.

import config from '../lib/env.mjs';
import { createLogger } from '../lib/log.mjs';
import { BlueBubblesClient } from '../lib/bluebubbles.mjs';
import * as dbapi from '../lib/db.mjs';

const log = createLogger('health');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Marker that identifies a pause this process owns. */
const AUTO_PREFIX = 'healthcheck:';
const isAutoPaused = (sender) => String(sender.paused_reason ?? '').startsWith(AUTO_PREFIX);

const failStreaks = new Map();
let shuttingDown = false;

export async function checkSender(sender) {
  const bb = BlueBubblesClient.forSender(sender);
  const res = await bb.ping();
  const streakKey = sender.id;

  if (res.alive) {
    failStreaks.set(streakKey, 0);

    if (sender.status === 'active') return { sender: sender.slug, alive: true, changed: false };

    if (!isAutoPaused(sender)) {
      // Operator-paused. Reachable, but not ours to reactivate.
      log.info('sender reachable but operator-paused - leaving alone', {
        sender: sender.slug,
        status: sender.status,
        paused_reason: sender.paused_reason,
      });
      return { sender: sender.slug, alive: true, changed: false, reason: 'operator_paused' };
    }

    await dbapi.setSenderStatus(sender.id, 'active');
    log.info('sender recovered - status back to active', { sender: sender.slug });
    return { sender: sender.slug, alive: true, changed: true, newStatus: 'active' };
  }

  const streak = (failStreaks.get(streakKey) ?? 0) + 1;
  failStreaks.set(streakKey, streak);

  log.warn('sender ping failed', {
    sender: sender.slug,
    streak,
    threshold: config.healthFailThreshold,
    classification: res.classification,
    httpStatus: res.httpStatus,
  });

  if (streak < config.healthFailThreshold) {
    return { sender: sender.slug, alive: false, changed: false, streak };
  }
  if (sender.status === 'failed') {
    return { sender: sender.slug, alive: false, changed: false, streak, already: true };
  }

  const reason =
    `${AUTO_PREFIX}bluebubbles unreachable at ${bb.baseUrl} ` +
    `(${res.classification}${res.httpStatus ? ` ${res.httpStatus}` : ''}) after ${streak} consecutive pings. ` +
    'Most likely cause: the Mac rebooted and nobody logged into the macOS user, ' +
    'so Messages and BlueBubbles never started (Phase 1 open item: no auto-login).';

  await dbapi.setSenderStatus(sender.id, 'failed', reason);

  // Sticky routing: this sender's contacts now simply stop receiving. Nothing is
  // rerouted. can_send() will answer sender_failed:<slug> for every one of them.
  log.error('SENDER DOWN - status flipped to failed, its traffic has STOPPED (no reroute)', {
    sender: sender.slug,
    url: bb.baseUrl,
    reason,
  });
  return { sender: sender.slug, alive: false, changed: true, newStatus: 'failed' };
}

export async function runOnce() {
  const senders = await dbapi.getSenders();
  if (senders.length === 0) {
    log.warn('no senders configured');
    return [];
  }
  const results = [];
  for (const s of senders) {
    try {
      results.push(await checkSender(s));
    } catch (err) {
      log.error('health check threw', { sender: s.slug, err });
      results.push({ sender: s.slug, error: true });
    }
  }
  return results;
}

async function main() {
  const once = process.argv.includes('--once');
  log.info('health checker starting', {
    intervalMs: config.healthIntervalMs,
    failThreshold: config.healthFailThreshold,
    once,
  });

  do {
    const results = await runOnce();
    log.info('health sweep', { results });
    if (once) break;
    // Sleep in slices so SIGTERM is honoured promptly.
    const deadline = Date.now() + config.healthIntervalMs;
    while (!shuttingDown && Date.now() < deadline) await sleep(500);
  } while (!shuttingDown);

  log.info('health checker stopped');
}

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    log.info('shutdown requested', { sig });
    shuttingDown = true;
  });
}

const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  main().then(
    () => process.exit(0),
    (err) => {
      log.error('health checker crashed', { err });
      process.exit(1);
    },
  );
}
