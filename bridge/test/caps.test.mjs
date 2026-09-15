// caps.test.mjs - what the worker DOES with each gate verdict.
//
// The one behaviour worth a deterministic test: a cap refusal must leave the
// message `queued` and must not touch BlueBubbles, while a suppression refusal
// must fail it. Both paths return before any send, so this runs with a
// BlueBubbles client that throws if it is called at all.
//
//   node --test --experimental-test-module-mocks test/caps.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { mock } from 'node:test';

import {
  classifyRefusal,
  retryAfterOf,
  CAP_REASONS,
  SENDER_WIDE_CAP_REASONS,
} from '../lib/caps.mjs';

// ---------------------------------------------------------------------------
// classification
// ---------------------------------------------------------------------------
test('cap refusals are never terminal', () => {
  for (const reason of CAP_REASONS) {
    const k = classifyRefusal(reason);
    assert.equal(k.cap, true, `${reason} should be a cap`);
    assert.equal(k.terminal, false, `${reason} must NOT fail the message`);
  }
});

test('only suppression and revoked consent are terminal', () => {
  assert.equal(classifyRefusal('suppressed:stop').terminal, true);
  assert.equal(classifyRefusal('suppressed:bounce').terminal, true);
  assert.equal(classifyRefusal('consent_revoked').terminal, true);

  for (const reason of [
    'in_flight_message_exists',
    'sender_paused:sender01',
    'consent_not_yet_effective',
    'sticky_sender_mismatch',
    'cap_daily',
    'quiet_hours',
  ]) {
    assert.equal(classifyRefusal(reason).terminal, false, `${reason} must not be terminal`);
  }
});

test('cap_new_conversation is a cap but not sender-wide', () => {
  // It refuses only FIRST CONTACTS. Replies into threads that already exist must
  // keep flowing behind it, so the worker must not stop the sender on it.
  assert.equal(classifyRefusal('cap_new_conversation').cap, true);
  assert.equal(classifyRefusal('cap_new_conversation').senderWide, false);
  assert.ok(!SENDER_WIDE_CAP_REASONS.has('cap_new_conversation'));

  for (const reason of ['cap_daily', 'cap_hourly', 'quiet_hours']) {
    assert.equal(classifyRefusal(reason).senderWide, true, `${reason} should stop the sender`);
  }
});

test('an unknown or missing reason is transient, not terminal', () => {
  assert.deepEqual(classifyRefusal(undefined), { terminal: false, cap: false, senderWide: false });
  assert.deepEqual(classifyRefusal('something_new'), { terminal: false, cap: false, senderWide: false });
});

test('retryAfterOf normalises the gate value', () => {
  assert.equal(retryAfterOf({ retry_after: '2026-09-16T08:00:00+00:00' }), '2026-09-16T08:00:00.000Z');
  assert.equal(retryAfterOf({ retry_after: null }), null);
  assert.equal(retryAfterOf({}), null);
  assert.equal(retryAfterOf({ retry_after: 'not a date' }), null);
});

// ---------------------------------------------------------------------------
// processMessage: the refusal branch, with the database mocked out
// ---------------------------------------------------------------------------
const SENDER = { id: 'sender-uuid', slug: 'sender01' };
const ROW = {
  id: 'msg-uuid',
  conversation_id: 'conv-uuid',
  conversations: { id: 'conv-uuid', provider_chat_guid: null, contact_id: 'contact-uuid' },
};

/** Any call on this is a test failure: a refused message must never reach BlueBubbles. */
const NEVER_SEND = new Proxy(
  {},
  {
    get(_t, prop) {
      return () => {
        throw new Error(`BlueBubbles.${String(prop)}() was called for a refused message`);
      };
    },
  },
);

async function runWithVerdict(verdict) {
  const calls = { markFailed: [], claimMessage: [] };

  mock.module('../lib/db.mjs', {
    exports: {
      canSendMessage: async () => verdict,
      markFailed: async (id, opts) => {
        calls.markFailed.push({ id, opts });
        return { id, state: 'failed' };
      },
      claimMessage: async (id) => {
        calls.claimMessage.push(id);
        return null; // never reached in these tests
      },
      isUniqueViolation: () => false,
      throwIf: () => {},
      db: {},
    },
  });

  // Imported AFTER the mock is installed, and with a unique query string so each
  // case gets a fresh module instance bound to its own mock.
  const { processMessage } = await import(`../worker/worker.mjs?case=${Math.random()}`);
  const result = await processMessage(NEVER_SEND, SENDER, ROW);
  mock.reset();
  return { result, calls };
}

test('a cap refusal leaves the message queued and fails nothing', async () => {
  for (const reason of ['cap_daily', 'cap_hourly', 'cap_new_conversation', 'quiet_hours']) {
    const { result, calls } = await runWithVerdict({
      allowed: false,
      reason,
      retry_after: '2026-09-16T08:00:00+00:00',
    });

    assert.equal(result.skipped, true, `${reason}: must skip`);
    assert.equal(result.reason, reason);
    assert.equal(result.capped, true);
    assert.equal(result.retryAfter, '2026-09-16T08:00:00.000Z');
    // THE point of this test. A cap says "not yet"; failing the message would
    // drop traffic that policy merely deferred.
    assert.equal(calls.markFailed.length, 0, `${reason}: must NOT fail the message`);
    assert.equal(calls.claimMessage.length, 0, `${reason}: must not even claim it`);
  }
});

test('a suppression refusal still fails the message', async () => {
  const { result, calls } = await runWithVerdict({ allowed: false, reason: 'suppressed:stop' });

  assert.equal(result.skipped, true);
  assert.equal(result.capped, false);
  assert.equal(calls.markFailed.length, 1);
  assert.equal(calls.markFailed[0].opts.errorCode, 'refused:suppressed:stop');
  // Nothing was ever handed to the provider, so there is no ambiguous outcome to
  // resolve: the row must not land in the Defect A triage queue.
  assert.equal(calls.markFailed[0].opts.reconciled, true);
});

test('a revoked-consent refusal fails the message', async () => {
  const { calls } = await runWithVerdict({ allowed: false, reason: 'consent_revoked' });
  assert.equal(calls.markFailed.length, 1);
  assert.equal(calls.markFailed[0].opts.errorCode, 'refused:consent_revoked');
});

test('an in-flight refusal leaves the message queued', async () => {
  const { result, calls } = await runWithVerdict({
    allowed: false,
    reason: 'in_flight_message_exists',
  });
  assert.equal(calls.markFailed.length, 0);
  assert.equal(result.capped, false);
  assert.equal(result.senderWide, false);
});
