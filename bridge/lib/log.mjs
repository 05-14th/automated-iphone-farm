// log.mjs - structured JSON lines to stdout. One event per line, no dependencies.
//
// Everything this bridge does is a forensic record: Defect A means the HTTP
// response carries no reliable information about delivery, so the only way to
// explain what happened after the fact is the log plus the send_attempts table.

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = LEVELS[process.env.LOG_LEVEL || 'info'] ?? LEVELS.info;

function redact(value) {
  if (typeof value !== 'string') return value;
  // Never let the BlueBubbles password or a bearer token reach the log.
  return value
    .replace(/(password=)[^&\s"']+/gi, '$1[redacted]')
    .replace(/(Bearer\s+)[A-Za-z0-9._\-]+/g, '$1[redacted]');
}

function scrub(obj, depth = 0) {
  if (depth > 6) return '[deep]';
  if (obj === null || obj === undefined) return obj;
  if (typeof obj === 'string') return redact(obj);
  if (Array.isArray(obj)) return obj.slice(0, 50).map((v) => scrub(v, depth + 1));
  if (obj instanceof Error) {
    return { name: obj.name, message: redact(obj.message), code: obj.code };
  }
  if (typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (/password|token|service_role|apikey/i.test(k)) {
        out[k] = '[redacted]';
        continue;
      }
      out[k] = scrub(v, depth + 1);
    }
    return out;
  }
  return obj;
}

function emit(level, component, msg, fields) {
  if (LEVELS[level] < threshold) return;
  const line = {
    ts: new Date().toISOString(),
    level,
    component,
    msg,
    ...(fields ? scrub(fields) : {}),
  };
  const stream = LEVELS[level] >= LEVELS.error ? process.stderr : process.stdout;
  stream.write(`${JSON.stringify(line)}\n`);
}

export function createLogger(component) {
  return {
    debug: (msg, fields) => emit('debug', component, msg, fields),
    info: (msg, fields) => emit('info', component, msg, fields),
    warn: (msg, fields) => emit('warn', component, msg, fields),
    error: (msg, fields) => emit('error', component, msg, fields),
    child: (sub) => createLogger(`${component}.${sub}`),
  };
}

export default createLogger;
