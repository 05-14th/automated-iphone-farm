#!/usr/bin/env node
// e2e.mjs - end-to-end test against the LIVE system: real Supabase, real
// BlueBubbles, a real iMessage to the repo's known-good recipient.
//
//   node test/e2e.mjs            # full run, cleans up its rows afterwards
//   node test/e2e.mjs --keep     # leave the rows in place for inspection
//   node test/e2e.mjs --no-live  # offline checks only (suppression + dedupe)
//
// DO NOT point this at gerryvienlifeflores@gmail.com, chilldaddygerry@gmail.com
// or thisyourman106@gmail.com. They are not registered with Apple; each send to
// one hangs 90-100s before failing with error=22.

import config from '../lib/env.mjs';
import * as dbapi from '../lib/db.mjs';
import { createServer } from '../api/server.mjs';
import { tick } from '../worker/worker.mjs';

const KEEP = process.argv.includes('--keep');
const LIVE = !process.argv.includes('--no-live');
const RUN = Date.now().toString(36).toUpperCase();

const RECIPIENT = process.env.TEST_RECIPIENT || 'meidy765@gmail.com';
const FAKE = `bridge-e2e-${RUN}@example.invalid`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
let created = { contacts: [], suppressions: [] };

function record(name, pass, detail) {
  results.push({ name, pass, detail });
  const tag = pass ? '\x1b[32mPASS\x1b[0m' : '\x1b[31mFAIL\x1b[0m';
  console.log(`${tag}  ${name}${detail ? `  -- ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`);
}

let server;
let base;
async function api(method, path, body) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(path === '/v1/webhooks/bluebubbles' ? {} : { Authorization: `Bearer ${config.apiToken}` }),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null;
  try { json = await res.json(); } catch { /* empty */ }
  return { status: res.status, body: json };
}

// ---------------------------------------------------------------------------
async function ensureSender() {
  const existing = await dbapi.getSenderBySlug('sender01');
  if (existing) return existing;
  const { data, error } = await dbapi.db
    .from('senders')
    .insert({
      slug: 'sender01',
      apple_email: process.env.SENDER01_EMAIL || 'meidy@clariocapital.com',
      bluebubbles_url: (process.env.BB_URL || 'http://127.0.0.1:12341').replace(/:\d+$/, ''),
      bluebubbles_port: Number((process.env.BB_URL || '').match(/:(\d+)/)?.[1] ?? 12341),
      macos_user: 'imsg01',
      status: 'active',
    })
    .select()
    .maybeSingle();
  dbapi.throwIf(error, 'ensureSender');
  console.log(`   seeded sender01 -> ${data.bluebubbles_url}:${data.bluebubbles_port}`);
  return data;
}

// ---------------------------------------------------------------------------
// T1 - can_send refuses a suppressed address
// ---------------------------------------------------------------------------
async function testSuppression() {
  const sup = await api('POST', '/v1/suppressions', { address: ` ${FAKE.toUpperCase()} `, reason: 'stop' });
  const normalizedOk = sup.body?.normalized_address === FAKE.toLowerCase();
  created.suppressions.push(FAKE.toLowerCase());
  record('T1a suppression created + address normalized', sup.status === 201 && normalizedOk, sup.body);

  const enq = await api('POST', '/v1/messages', { to: FAKE, body: `BRIDGE-E2E-${RUN} should never send` });
  created.contacts.push(FAKE);
  const refused = enq.status === 403 && String(enq.body?.reason).startsWith('suppressed:');
  record('T1b can_send refuses a suppressed address', refused, { status: enq.status, ...enq.body });

  const { count } = await dbapi.db
    .from('messages')
    .select('id', { count: 'exact', head: true })
    .eq('body', `BRIDGE-E2E-${RUN} should never send`);
  record('T1c nothing was enqueued for the suppressed address', (count ?? 0) === 0, { rows: count });
}

// ---------------------------------------------------------------------------
// T2 - duplicate webhook with an identical provider GUID collapses to one row
// ---------------------------------------------------------------------------
async function testWebhookDedupe() {
  const guid = `E2E-DUP-${RUN}`;
  const payload = {
    type: 'new-message',
    data: {
      guid,
      text: `inbound dedupe probe ${RUN}`,
      isFromMe: false,
      dateCreated: Date.now(),
      dateDelivered: Date.now(),
      handle: { address: FAKE },
      chats: [{ guid: `iMessage;-;${FAKE}` }],
      error: 0,
    },
  };

  const r1 = await api('POST', '/v1/webhooks/bluebubbles', payload);
  const r2 = await api('POST', '/v1/webhooks/bluebubbles', payload);

  record('T2a first webhook accepted, not deduped', r1.status === 200 && r1.body?.deduped === false, r1.body);
  record('T2b duplicate webhook reported as deduped', r2.status === 200 && r2.body?.deduped === true, r2.body);

  const { count } = await dbapi.db
    .from('messages')
    .select('id', { count: 'exact', head: true })
    .eq('provider_guid', guid);
  record('T2c exactly one row for the duplicated provider_guid', count === 1, { rows: count });
}

// ---------------------------------------------------------------------------
// T3 - the real thing: enqueue -> worker -> delivered iMessage
// ---------------------------------------------------------------------------
async function waitForState(id, wanted, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const m = await dbapi.getMessage(id);
    if (wanted.includes(m.state)) return m;
    await sleep(1500);
  }
  return dbapi.getMessage(id);
}

async function testLiveSend() {
  const text1 = `BRIDGE-E2E-${RUN}-A`;

  const enq = await api('POST', '/v1/messages', { to: RECIPIENT, body: text1 });
  if (enq.status === 403 && enq.body?.reason === 'no_consent_on_record') {
    // Expected on a first run: can_send() refuses without a consent row.
    record('T3a can_send refuses with no consent on record', true, enq.body);
    const c = await api('POST', '/v1/consents', {
      address: RECIPIENT,
      status: 'granted',
      source: 'operator',
      evidence: { note: 'repo known-good test recipient; clarioinc own iMessage alias', run: RUN },
    });
    record('T3b consent recorded', c.status === 201, c.body);
  } else {
    record('T3a can_send verdict for recipient', enq.status === 202, { status: enq.status, ...enq.body });
  }
  created.contacts.push(RECIPIENT);

  const enq2 = enq.status === 202 ? enq : await api('POST', '/v1/messages', { to: RECIPIENT, body: text1 });
  if (enq2.status !== 202) {
    record('T3c enqueue accepted (202)', false, { status: enq2.status, ...enq2.body });
    return;
  }
  record('T3c enqueue accepted (202)', true, { id: enq2.body.id, temp_guid: enq2.body.temp_guid });

  const conv = await dbapi.db.from('conversations').select('*').eq('id', enq2.body.conversation_id).maybeSingle();
  const path1 = conv.data?.provider_chat_guid ? 'message/text (fast)' : 'chat/new (slow, expect timeout)';
  console.log(`   message 1 will take the ${path1} path`);

  // Run the worker inline, one tick. chat/new can take 150s, so give it room.
  const t0 = Date.now();
  const workerRun = tick();
  const m1 = await waitForState(enq2.body.id, ['sent'], 200_000);
  await workerRun.catch(() => {});
  const elapsed = Math.round((Date.now() - t0) / 1000);

  const attempts1 = await dbapi.getAttempts(enq2.body.id);
  record(
    `T3d message 1 reached sent (${path1}, ${elapsed}s)`,
    m1.state === 'sent',
    { state: m1.state, provider_guid: m1.provider_guid, reconciled: m1.reconciled,
      attempts: attempts1.map((a) => a.outcome) },
  );
  record('T3e message 1 has a provider GUID', Boolean(m1.provider_guid), m1.provider_guid);

  const conv2 = await dbapi.db.from('conversations').select('*').eq('id', enq2.body.conversation_id).maybeSingle();
  record('T3f conversation now carries provider_chat_guid', Boolean(conv2.data?.provider_chat_guid),
    conv2.data?.provider_chat_guid);

  // ---- second message: must take the FAST path -----------------------------
  if (!conv2.data?.provider_chat_guid) {
    record('T3g fast path (message/text) on the second send', false, 'skipped: no chat guid recorded');
    return;
  }
  const text2 = `BRIDGE-E2E-${RUN}-B`;
  const enq3 = await api('POST', '/v1/messages', { to: RECIPIENT, body: text2 });
  if (enq3.status !== 202) {
    record('T3g fast path (message/text) on the second send', false, { status: enq3.status, ...enq3.body });
    return;
  }
  const t1 = Date.now();
  const run2 = tick();
  const m2 = await waitForState(enq3.body.id, ['sent', 'failed'], 90_000);
  await run2.catch(() => {});
  const secs = ((Date.now() - t1) / 1000).toFixed(1);
  const attempts2 = await dbapi.getAttempts(enq3.body.id);
  record(
    `T3g message 2 sent via the fast path in ${secs}s`,
    m2.state === 'sent' && attempts2.some((a) => a.outcome === 'accepted'),
    { state: m2.state, provider_guid: m2.provider_guid, attempts: attempts2.map((a) => a.outcome) },
  );

  // ---- the DB row itself ---------------------------------------------------
  const { data: rows } = await dbapi.db
    .from('messages')
    .select('id, state, direction, body, provider_guid, reconciled, sent_at')
    .in('body', [text1, text2]);
  console.log('   DB rows:', JSON.stringify(rows, null, 2));
  record('T3h both messages present in the DB as sent outbound rows',
    rows?.length === 2 && rows.every((r) => r.state === 'sent' && r.direction === 'outbound'),
    rows?.map((r) => ({ state: r.state, guid: r.provider_guid })));
}

// ---------------------------------------------------------------------------
async function testHealth() {
  const h = await api('GET', '/v1/health');
  record('T4 /v1/health reports sender + queue depth',
    h.status === 200 && Array.isArray(h.body?.senders) && h.body?.queue !== undefined, h.body);

  const unauth = await fetch(`${base}/v1/health`);
  record('T5 unauthenticated request rejected', unauth.status === 401, { status: unauth.status });
}

// ---------------------------------------------------------------------------
async function cleanup() {
  if (KEEP) {
    console.log('\n--keep: leaving test rows in place.');
    return;
  }
  for (const addr of created.contacts) {
    const c = await dbapi.getContactByAddress(addr);
    if (!c) continue;
    // contacts cascade to conversations -> messages -> send_attempts, and to consents.
    await dbapi.db.from('contacts').delete().eq('id', c.id);
  }
  for (const addr of created.suppressions) {
    await dbapi.db.from('suppressions').delete().eq('normalized_address', addr);
  }
  // The dedupe probe's inbound row hangs off the fake contact and went with it.
  console.log('\ncleaned up test rows (contacts + suppressions cascaded).');
}

// ---------------------------------------------------------------------------
async function main() {
  console.log(`\n=== bridge e2e, run ${RUN} ===`);
  console.log(`recipient: ${RECIPIENT}   live sends: ${LIVE}\n`);

  await ensureSender();

  server = createServer();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
  console.log(`   gateway on ${base}\n`);

  try {
    await testSuppression();
    await testWebhookDedupe();
    await testHealth();
    if (LIVE) await testLiveSend();
    else console.log('\n(--no-live: skipped the real send)');
  } finally {
    await cleanup().catch((e) => console.error('cleanup failed:', e));
    server.close();
  }

  const failed = results.filter((r) => !r.pass);
  console.log(`\n=== ${results.length - failed.length}/${results.length} passed ===`);
  if (failed.length) {
    console.log('FAILURES:');
    for (const f of failed) console.log(`  - ${f.name}: ${JSON.stringify(f.detail)}`);
  }
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  server?.close();
  process.exit(1);
});
