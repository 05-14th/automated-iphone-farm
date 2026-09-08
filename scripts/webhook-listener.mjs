#!/usr/bin/env node
// webhook-listener.mjs — receives BlueBubbles webhook POSTs and appends one JSON line per event
// to logs/events.jsonl. Built-ins only (Node >= 18).
//
// BlueBubbles posts `{ type: string, data: any }` (see server/services/webhookService/index.ts).
// For message events `data` is a serialized message: { guid, text, dateCreated (ms epoch), isFromMe,
// handle: { address }, chats: [{ guid }], ... }.
//
// Endpoints:
//   POST /                    ingest event (any path accepted)
//   GET  /health              { ok, port, logFile, eventCount, uptimeSec }
//   GET  /events?since=<iso>&text=<substring>&type=<type>&guid=<guid>&chatGuid=<guid>&isFromMe=<bool>&limit=<n>
//                             -> JSON array of matching log lines (oldest first)
//
// Config: WEBHOOK_PORT (default 12391), WEBHOOK_HOST (default 127.0.0.1), LOG_DIR (default <repo>/logs),
//         --port N / --log-dir DIR CLI flags. Values are also read from <repo>/.env if not already set.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = path.resolve(path.dirname(__filename), '..');

// Load .env (simple KEY=VALUE, no expansion) without overriding real env vars.
function loadDotEnv(file) {
  try {
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
      const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (!m) continue;
      let v = m[2];
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
      if (process.env[m[1]] === undefined) process.env[m[1]] = v;
    }
  } catch { /* no .env — fine */ }
}
loadDotEnv(path.join(REPO_ROOT, '.env'));

const argv = process.argv.slice(2);
function flag(name, dflt) {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : dflt;
}
const PORT = Number(flag('--port', process.env.WEBHOOK_PORT || 12391));
const HOST = flag('--host', process.env.WEBHOOK_HOST || '127.0.0.1');
const LOG_DIR = path.resolve(flag('--log-dir', process.env.LOG_DIR || path.join(REPO_ROOT, 'logs')));
const LOG_FILE = path.join(LOG_DIR, 'events.jsonl');
const MAX_BODY = 5 * 1024 * 1024;

fs.mkdirSync(LOG_DIR, { recursive: true });
const startedAt = Date.now();
let eventCount = 0;

const log = (...a) => console.log(new Date().toISOString(), ...a);

function toIso(ms) {
  if (ms === null || ms === undefined || ms === '') return null;
  const n = Number(ms);
  return Number.isFinite(n) ? new Date(n).toISOString() : String(ms);
}

// Normalize a BlueBubbles webhook payload into a flat, greppable record.
function normalize(payload, receivedAt) {
  const type = payload?.type ?? null;
  const d = payload?.data ?? null;
  const isObj = d && typeof d === 'object';
  const chatGuid =
    (isObj && Array.isArray(d.chats) && d.chats[0]?.guid) ||
    (isObj && d.chat?.guid) ||
    (isObj && d.chatGuid) ||
    null;
  const handle =
    (isObj && d.handle?.address) ||
    (isObj && typeof d.handle === 'string' ? d.handle : null) ||
    (isObj && d.address) ||
    null;
  return {
    receivedAt,
    type,
    guid: isObj ? (d.guid ?? d.tempGuid ?? null) : null,
    chatGuid,
    text: isObj ? (d.text ?? d.message?.text ?? null) : (typeof d === 'string' ? d : null),
    isFromMe: isObj ? (d.isFromMe ?? null) : null,
    dateCreated: isObj ? (d.dateCreated ?? null) : null,
    dateCreatedIso: isObj ? toIso(d.dateCreated) : null,
    handle,
    raw: payload,
  };
}

function appendLine(rec) {
  fs.appendFileSync(LOG_FILE, JSON.stringify(rec) + '\n');
  eventCount++;
}

function readEvents() {
  let text;
  try { text = fs.readFileSync(LOG_FILE, 'utf8'); } catch { return []; }
  const out = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    try { out.push(JSON.parse(line)); } catch { /* skip corrupt line */ }
  }
  return out;
}

function queryEvents(q) {
  const since = q.get('since');
  const sinceMs = since ? Date.parse(since) : NaN;
  const textQ = q.get('text');
  const typeQ = q.get('type');
  const guidQ = q.get('guid');
  const chatQ = q.get('chatGuid');
  const fromMeQ = q.get('isFromMe');
  const limit = Number(q.get('limit') || 0);
  let rows = readEvents().filter((e) => {
    if (!Number.isNaN(sinceMs) && Date.parse(e.receivedAt) < sinceMs) return false;
    if (textQ && !(typeof e.text === 'string' && e.text.includes(textQ))) return false;
    if (typeQ && e.type !== typeQ) return false;
    if (guidQ && e.guid !== guidQ) return false;
    if (chatQ && e.chatGuid !== chatQ) return false;
    if (fromMeQ !== null && fromMeQ !== undefined && fromMeQ !== '') {
      const want = fromMeQ === 'true' || fromMeQ === '1';
      if (Boolean(e.isFromMe) !== want) return false;
    }
    return true;
  });
  if (limit > 0) rows = rows.slice(-limit);
  return rows;
}

function send(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);

  if (req.method === 'GET' && url.pathname === '/health') {
    return send(res, 200, {
      ok: true, port: PORT, host: HOST, logFile: LOG_FILE,
      eventCount, uptimeSec: Math.round((Date.now() - startedAt) / 1000), now: new Date().toISOString(),
    });
  }
  if (req.method === 'GET' && url.pathname === '/events') {
    try { return send(res, 200, queryEvents(url.searchParams)); }
    catch (e) { return send(res, 500, { ok: false, error: String(e) }); }
  }
  if (req.method === 'POST') {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      const receivedAt = new Date().toISOString();
      const bodyText = Buffer.concat(chunks).toString('utf8');
      let payload;
      try { payload = JSON.parse(bodyText); }
      catch {
        payload = { type: 'unparseable', data: bodyText };
      }
      const rec = normalize(payload, receivedAt);
      try {
        appendLine(rec);
        log(`event type=${rec.type} guid=${rec.guid} chat=${rec.chatGuid} fromMe=${rec.isFromMe} handle=${rec.handle} text=${JSON.stringify(rec.text ?? '').slice(0, 120)}`);
        send(res, 200, { ok: true });
      } catch (e) {
        log('ERROR appending event:', e);
        send(res, 500, { ok: false, error: String(e) });
      }
    });
    req.on('error', (e) => { log('request error:', e.message); });
    return;
  }
  send(res, 404, { ok: false, error: 'not found' });
});

server.listen(PORT, HOST, () => {
  log(`webhook-listener listening on http://${HOST}:${PORT}  log=${LOG_FILE}`);
  log(`register in BlueBubbles: scripts/bb.sh webhook-add http://${HOST}:${PORT}/`);
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => { log(`${sig} received, shutting down`); server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 1000).unref(); });
}
