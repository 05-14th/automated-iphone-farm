// env.mjs - configuration loading. No dependency on dotenv.
//
// Two .env files are read, in this order (earlier wins, real process env wins over both):
//   1. bridge/.env      - Supabase keys, API token, worker pacing
//   2. <repo root>/.env  - BB_URL / BB_PASSWORD, owned by Phase 1
//
// The BlueBubbles password deliberately lives in exactly ONE file (the repo root),
// and is read from there rather than duplicated into bridge/.env.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const BRIDGE_DIR = path.resolve(HERE, '..');
export const REPO_ROOT = path.resolve(BRIDGE_DIR, '..');

function loadDotEnv(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return false;
  }
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m) continue;
    let v = m[2];
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    if (process.env[m[1]] === undefined) process.env[m[1]] = v;
  }
  return true;
}

loadDotEnv(path.join(BRIDGE_DIR, '.env'));

const rootEnvRel = process.env.BRIDGE_ROOT_ENV || '.env';
loadDotEnv(path.isAbsolute(rootEnvRel) ? rootEnvRel : path.join(REPO_ROOT, rootEnvRel));

function req(name) {
  const v = process.env[name];
  if (!v) {
    throw new Error(
      `Missing required config ${name}. Copy bridge/.env.example to bridge/.env and fill it in.`,
    );
  }
  return v;
}

const num = (name, dflt) => {
  const v = process.env[name];
  const n = v === undefined || v === '' ? dflt : Number(v);
  if (!Number.isFinite(n)) throw new Error(`Config ${name} must be a number, got ${v}`);
  return n;
};

export const config = {
  supabaseUrl: req('SUPABASE_URL'),
  supabaseServiceRoleKey: req('SUPABASE_SERVICE_ROLE_KEY'),

  apiToken: process.env.BRIDGE_API_TOKEN || '',
  apiHost: process.env.BRIDGE_API_HOST || '127.0.0.1',
  apiPort: num('BRIDGE_API_PORT', 12350),

  bbUrl: process.env.BB_URL || 'http://127.0.0.1:12341',
  bbPassword: req('BB_PASSWORD'),

  workerMinIntervalMs: num('WORKER_MIN_INTERVAL_MS', 8000),
  workerMaxIntervalMs: num('WORKER_MAX_INTERVAL_MS', 12000),
  workerIdlePollMs: num('WORKER_IDLE_POLL_MS', 3000),

  bbTimeoutMessageMs: num('BB_TIMEOUT_MESSAGE_MS', 30000),
  bbTimeoutChatNewMs: num('BB_TIMEOUT_CHATNEW_MS', 150000),
  bbTimeoutQueryMs: num('BB_TIMEOUT_QUERY_MS', 20000),
  reconcileSettleMs: num('RECONCILE_SETTLE_MS', 5000),

  healthIntervalMs: num('HEALTH_INTERVAL_MS', 60000),
  healthTimeoutMs: num('HEALTH_TIMEOUT_MS', 10000),
  healthFailThreshold: num('HEALTH_FAIL_THRESHOLD', 2),

  logLevel: process.env.LOG_LEVEL || 'info',
};

export default config;
