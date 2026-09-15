// caps.mjs - how the worker must react to each verdict from the pre-send gate.
//
// The gate (`can_send_message()` -> `can_send()`) decides WHETHER a message may
// go out. This module decides what the worker DOES with a refusal, and the
// distinction it draws is the one thing in the send loop that is easy to get
// wrong and expensive to get wrong:
//
//   * a TERMINAL refusal must fail the message. Suppression and revoked consent
//     never clear on their own, so leaving such a message queued would block its
//     conversation's FIFO head forever and stall every later message to that
//     contact.
//
//   * a CAP refusal must NOT fail the message. "You have sent 200 messages
//     today" and "it is 2 a.m. where this sender lives" are not errors; they are
//     "not yet". The row stays `queued` - genuinely queued, not a new state -
//     and goes out unchanged when the window reopens. Failing it would silently
//     drop traffic that policy merely deferred, and (because failed -> queued
//     needs `reconciled = true`) would need a reconciliation pass to undo a
//     situation where nothing was ever sent.
//
// Everything else - a paused sender, an in-flight message, consent that is not
// yet effective - is also transient and also stays queued. Terminal is the
// narrow, explicitly enumerated case.

/** Refusals that can never clear on their own. These, and only these, fail the message. */
export const TERMINAL_REASON_PATTERNS = [/^suppressed:/, /^consent_revoked$/];

/**
 * Operating-cap and quiet-hours refusals [guide step 36]. All temporary, all
 * carrying a `retry_after` in the verdict.
 */
export const CAP_REASONS = new Set([
  'cap_daily',
  'cap_hourly',
  'cap_new_conversation',
  'quiet_hours',
]);

/**
 * Cap refusals that apply to the SENDER as a whole rather than to one message.
 *
 * `cap_new_conversation` is deliberately absent: it refuses only first contacts,
 * and a sender that has spent its new-conversation allowance can still reply
 * into every thread it already has open. Treating it as sender-wide would stop
 * that legitimate traffic dead.
 */
export const SENDER_WIDE_CAP_REASONS = new Set(['cap_daily', 'cap_hourly', 'quiet_hours']);

/**
 * Classify one gate verdict.
 *
 * @param {string|undefined|null} reason
 * @returns {{terminal: boolean, cap: boolean, senderWide: boolean}}
 */
export function classifyRefusal(reason) {
  const r = reason ?? '';
  return {
    terminal: TERMINAL_REASON_PATTERNS.some((re) => re.test(r)),
    cap: CAP_REASONS.has(r),
    senderWide: SENDER_WIDE_CAP_REASONS.has(r),
  };
}

/** `retry_after` as an ISO string, or null. The gate returns it on every cap refusal. */
export function retryAfterOf(verdict) {
  const value = verdict?.retry_after;
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}
