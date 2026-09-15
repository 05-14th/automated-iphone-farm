# Deploying the Python API on a separate server

This service is designed to run **away from the Mac mini**, on an ordinary Linux
box with a public address. Nothing about it needs to be near Messages.app.

---

## 1. Why BlueBubbles must stay on loopback

Read this before you decide to "just open the port".

BlueBubbles on the Mac mini listens on `127.0.0.1:12341` and its proxy service is
set to `dynamic-dns → 127.0.0.1`. That is not an oversight. BlueBubbles drives a
signed-in Apple Account through AppleScript with Full Disk Access; anything that
can reach it can read every conversation on that identity and send as it. Its
only protection is a shared password, over plain HTTP.

Two further reasons specific to this stack:

* **Defect A** means the HTTP response carries no reliable information about
  delivery. Any remote caller would have to reimplement reconciliation against
  provider state to know what happened — and reconciliation needs the database,
  which is where this design already puts it.
* **Per-conversation FIFO and the 8–12 s pacing** are enforced by the database
  and the single worker. A second caller reaching BlueBubbles directly bypasses
  both, and Apple's tolerance for a young sender identity is the thing you would
  be spending.

So: **there is no deployment topology in which this Python API talks to
BlueBubbles.** It writes to Supabase. The worker on the Mac reads Supabase. If
you ever find yourself wanting a tunnel from this server to port 12341, the
answer is that the feature you want belongs in the worker.

```
   INTERNET          THIS SERVER                SUPABASE            MAC MINI
   ────────    ┌──────────────────────┐    ┌──────────────┐   ┌──────────────────┐
   caller ───▶ │ nginx/Caddy :443 TLS │    │              │   │ worker (node)    │
               │          │           │    │  messages    │◀──┤   │              │
               │          ▼           │───▶│  contacts    │   │   ▼              │
               │ uvicorn 127.0.0.1:8080    │  consents    │   │ BlueBubbles      │
               │ (this FastAPI app)   │◀───│  …           │──▶│ 127.0.0.1:12341  │
               └──────────────────────┘    └──────────────┘   │   │              │
                                                              │   ▼              │
                     outbound 443 only ────────────────────▶  │ Messages.app     │
                                                              └──────────────────┘
```

---

## 2. What must keep running on the Mac

The API is useless on its own. These must be up on the mini, under the `imsg01`
console session:

| | What | Check |
|---|---|---|
| 1 | `imsg01` logged in | A reboot leaves the Mac at a login window and the sender **completely offline** until a human logs in — no auto-login is configured. This is a standing outage risk (Phase 1 [21], still open). |
| 2 | Messages.app, signed in | `ps -o user,pid -ax \| grep Messages` |
| 3 | BlueBubbles on 12341 | `curl -s "http://127.0.0.1:12341/api/v1/ping?password=…"` |
| 4 | `bridge/worker/worker.mjs` | The only thing that sends. Without it, messages queue forever and this API keeps returning 202. |
| 5 | `bridge/worker/health.mjs` | Flips a sender to `failed` when its BlueBubbles instance dies. Without it, `senders.status` reads `active` into a void. |
| 6 | `bridge/api/server.mjs` | The loopback gateway BlueBubbles posts its webhook to. **Without it, no inbound message is ever recorded**, and `GET /v1/inbox` stays empty forever. |

Item 6 is the one people forget. This Python API has no webhook: BlueBubbles is
on the Mac's loopback and cannot reach this server.

---

## 3. Server setup

Ubuntu 24.04 or similar. Python 3.12+.

```bash
sudo adduser --system --group --home /opt/imessage-api imessage
sudo -u imessage git clone <repo> /opt/imessage-api/app
cd /opt/imessage-api/app/api
sudo -u imessage python3 -m venv .venv
sudo -u imessage .venv/bin/pip install -r requirements.txt
```

### Secrets

```bash
sudo -u imessage cp .env.example .env
sudo -u imessage chmod 600 .env
sudo -u imessage vim .env
```

* `SUPABASE_SERVICE_ROLE_KEY` — `supabase projects api-keys --project-ref <ref>`.
  This key is `BYPASSRLS`. It is a database root credential. It must never reach
  a browser, a log, a ticket or a client bundle.
* `API_TOKEN` — `python3 -c "import secrets; print(secrets.token_hex(32))"`.
* `API_HOST=127.0.0.1` — bind loopback. The reverse proxy is the only thing that
  should reach uvicorn.

If your platform has a secret store (systemd credentials, Vault, SSM), prefer it
over a file. `EnvironmentFile=` below is the simple option, not the best one.

---

## 4. systemd unit

`/etc/systemd/system/imessage-api.service`:

```ini
[Unit]
Description=iMessage Bridge API (FastAPI)
Documentation=file:///opt/imessage-api/app/api/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=imessage
Group=imessage
WorkingDirectory=/opt/imessage-api/app/api
EnvironmentFile=/opt/imessage-api/app/api/.env
ExecStart=/opt/imessage-api/app/api/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8080 \
    --workers 2 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \
    --timeout-graceful-shutdown 20 \
    --no-access-log

# Graceful: SIGTERM stops accepting, drains in-flight requests, then the lifespan
# handler closes the DB pool. Nothing can be lost - this process never holds a send.
KillSignal=SIGTERM
TimeoutStopSec=30
Restart=on-failure
RestartSec=5

# Hardening. This service needs nothing but outbound 443 and its own directory.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallFilter=@system-service
SystemCallArchitectures=native
UMask=0077
ReadWritePaths=

StandardOutput=journal
StandardError=journal
SyslogIdentifier=imessage-api

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now imessage-api
journalctl -u imessage-api -f          # JSON lines, tokens redacted
```

**On `--workers 2`:** workers are independent processes with independent HTTP
pools. That is safe here because this service holds no state — ordering and
mutual exclusion live in the database (`messages_one_sending_per_conversation_uidx`,
`can_send()`), not in the process. Scale workers freely; do **not** run a second
*worker on the Mac* on the same sender.

---

## 5. Reverse proxy with TLS

### Caddy (automatic certificates)

```caddyfile
api.example.com {
    encode gzip

    # Rate limit at the edge: the Mac sends one message per 8-12 s per sender,
    # so there is nothing to gain from letting a caller post faster than this.
    @v1 path /v1/*
    rate_limit @v1 {
        zone api { key {remote_host}  events 120  window 1m }
    }

    reverse_proxy 127.0.0.1:8080 {
        header_up X-Real-IP {remote_host}
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        -Server
    }
}
```

### nginx

```nginx
limit_req_zone $binary_remote_addr zone=api:10m rate=2r/s;

server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;

    client_max_body_size 1m;          # bulk of 100 is ~40 KB; nothing legitimate is larger

    location /v1/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}

server {
    listen 80;
    server_name api.example.com;
    return 301 https://$host$request_uri;
}
```

`--proxy-headers --forwarded-allow-ips 127.0.0.1` in the unit makes uvicorn trust
`X-Forwarded-*` **only** from the local proxy. Without that restriction any
caller could forge the client IP that lands in the suppression-deletion audit.

**Terminate TLS. The bearer token is a static credential** — one interception is
permanent compromise.

---

## 6. Firewall

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

* Inbound: **443 only** (and SSH, ideally restricted to your own addresses).
* Port 8080 must never be reachable from outside. The bind address already
  prevents it; the firewall is the second line.
* Outbound: **443 to Supabase** is the only thing this service needs. If you
  filter egress, allow `<ref>.supabase.co:443` and nothing else.
* There is **no** firewall rule to the Mac mini, in either direction. If one
  exists, remove it — it is not part of this design.

---

## 7. Operating notes

**Rotating `API_TOKEN`:** update `.env`, `systemctl restart imessage-api`,
update callers. There is no overlap window — the service holds exactly one token.
If you need zero-downtime rotation, run two instances behind the proxy and cut
over.

**Rotating the Supabase key:** rotate in Supabase, update `.env` on this server
**and** `bridge/.env` on the Mac, restart both. The worker and the API use the
same `service_role` key.

**Monitoring:** poll `GET /v1/health` (no auth). Alert on:
* `database: false` — this server cannot reach Supabase.
* `queue.unreconciled_failed > 0` — Defect A triage; no retry sweep is safe.
* `queue.sending` stuck above 0 for more than ~3 minutes — the worker is wedged
  or the Mac is at a login window.
* No `active` sender.

**Logs:** JSON lines on stdout, into the journal. Bearer tokens, Supabase keys
and JWT-shaped strings are redacted by the formatter before they are written. Log
the journal to your aggregator; do not log the reverse proxy's request bodies —
message bodies are recipient content.

**Backups:** this service is stateless. Everything durable is in Supabase; back
that up, not this box.

**`/docs` on a public host:** the schema exposes route shapes, not data, and all
routes still require the token. Set `DOCS_ENABLED=false` if you would rather not
publish it.

**CORS:** off unless `CORS_ORIGINS` is set. Think twice — enabling it means a
browser is holding `API_TOKEN`, and a token in front-end code is a public token.
Put your own backend in front instead.

---

## 8. Deployment checklist

Before the first real message:

- [ ] Python 3.12+ and `requirements.txt` installed into `api/.venv`
- [ ] `api/.env` written, `chmod 600`, owned by the service user
- [ ] `API_TOKEN` is 32 random bytes, not a guessable string
- [ ] `SUPABASE_SERVICE_ROLE_KEY` is the **service_role** key, not the anon key
- [ ] `API_HOST=127.0.0.1` — uvicorn is not directly exposed
- [ ] Migration `20260915210000_phase13_scheduled_send_and_audit.sql` applied (`supabase migration list`)
- [ ] systemd unit installed, enabled, `systemctl status` clean
- [ ] Reverse proxy live with a valid certificate; HTTP redirects to HTTPS
- [ ] `--proxy-headers --forwarded-allow-ips 127.0.0.1` set
- [ ] Rate limiting configured at the proxy
- [ ] Firewall: inbound 443 (+ SSH) only; port 8080 unreachable from outside
- [ ] `curl https://api.example.com/v1/health` returns `status: ok` with a sender listed
- [ ] `curl` without a token returns **401**, with the token returns data
- [ ] On the Mac: `imsg01` logged in, Messages signed in, BlueBubbles on 12341
- [ ] On the Mac: `worker.mjs`, `health.mjs` **and** `api/server.mjs` all running
- [ ] BlueBubbles webhook points at the Mac's loopback gateway (`http://127.0.0.1:12350/v1/webhooks/bluebubbles`)
- [ ] **Decided** on the `scheduled_for` worker change (README §7). Until it is
      made, `mode=scheduled` sends immediately.
- [ ] Monitoring alerts on `unreconciled_failed > 0` and on a stuck `sending`
- [ ] Consent recorded for every address you intend to message, and reviewed by
      counsel for each jurisdiction. `can_send()` records what happened; it does
      not decide what is lawful.
