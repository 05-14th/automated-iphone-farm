# Phase 1 toolkit — BlueBubbles / iMessage acceptance testing

Scripts for driving and verifying the Mac mini deployment described in
`Email_Based_BlueBubbles_iMessage_Deployment_Guide.pdf`. Step numbers below refer to that guide.

## Topology

| Role | macOS user | What runs there |
|---|---|---|
| Sender 01 | `imsg01` | Messages.app signed into Apple Account 01 (`SENDER01_EMAIL`), BlueBubbles Server v1.9.9 on `http://127.0.0.1:12341` (password auth) |
| Automation + tester | `clarioinc` | this repo, the webhook listener (`127.0.0.1:$WEBHOOK_PORT`), and clarioinc's own Messages.app signed into a *different* Apple ID (`TEST_RECIPIENT`) used as the consenting test recipient / inbound sender |

Everything in `scripts/` except `scripts/imsg01/` runs as `clarioinc`. Nothing needs `sudo`.
Requirements: `bash`, `curl`, `jq`, `node` (>= 18, built-ins only), `osascript`, `perl` (all present on this Mac).

## Configuration: `.env`

Copy `.env.example` to `.env` (already done; `.env` is gitignored, mode 600) and fill in:

| Var | Meaning |
|---|---|
| `BB_URL` | BlueBubbles Server URL, `http://127.0.0.1:12341` |
| `BB_PASSWORD` | Server password. A strong random one was generated with `openssl rand -base64 24` — paste it into BlueBubbles Server > Settings > Server Password (step 12). |
| `SENDER01_EMAIL` | Apple Account 01's iMessage email identity |
| `TEST_RECIPIENT` | clarioinc's iMessage handle (the consenting tester) |
| `WEBHOOK_PORT` | Listener port, default `12391` |

Explicit environment variables take precedence over `.env` (e.g. `BB_URL=... scripts/bb.sh ping`).

## Scripts

### `scripts/bb.sh` — BlueBubbles REST wrapper
Routes verified against `bluebubbles-server` source (`api/http/api/v1/httpRoutes.ts`); every request carries `?password=`.

| Command | HTTP | Notes |
|---|---|---|
| `bb.sh ping` | `GET /api/v1/ping` | |
| `bb.sh info` | `GET /api/v1/server/info` | version, private_api, helper_connected |
| `bb.sh chats [limit]` | `POST /api/v1/chat/query` | participants + last message, newest first |
| `bb.sh chat <guid>` | `GET /api/v1/chat/<guid>?with=participants,lastmessage` | |
| `bb.sh find-chat <address>` | chat/query | prints the 1:1 chat guid for an address (prefers iMessage) |
| `bb.sh send <chatGuid> <text>` | `POST /api/v1/message/text` | `method=apple-script`, client `tempGuid` (echoed back in output) |
| `bb.sh new-chat <address> <text>` | `POST /api/v1/chat/new` | `addresses=[address]`, `service=iMessage`, `method=apple-script` |
| `bb.sh messages <chatGuid> [limit]` | `GET /api/v1/chat/<guid>/message?sort=DESC` | guid/text/dateCreated/isFromMe/handle/dateDelivered/error |
| `bb.sh messages-raw <chatGuid> [limit]` | same | full payload |
| `bb.sh webhooks` | `GET /api/v1/webhook` | |
| `bb.sh webhook-add <url> [events]` | `POST /api/v1/webhook` | default events `new-message,updated-message,server-update`; pass `'*'` for all |
| `bb.sh webhook-del <id>` | `DELETE /api/v1/webhook/<id>` | |
| `bb.sh raw <METHOD> <path> [json]` | any | escape hatch |

Failures print `bb.sh: HTTP <status>` plus the server body to stderr and exit 1.

### `scripts/webhook-listener.mjs` — webhook receiver
Node HTTP server on `127.0.0.1:$WEBHOOK_PORT`. Every BlueBubbles POST (`{type, data}`) becomes one line in `logs/events.jsonl`:
`receivedAt, type, guid, chatGuid, text, isFromMe, dateCreated, dateCreatedIso, handle, raw`.

- `GET /health` -> `{ok, port, eventCount, uptimeSec}`
- `GET /events?since=<iso>&text=<substr>&type=<t>&guid=<g>&chatGuid=<g>&isFromMe=<bool>&limit=<n>` -> JSON array
- Run ad hoc: `node scripts/webhook-listener.mjs [--port N] [--log-dir DIR]`
- Register with BlueBubbles: `scripts/bb.sh webhook-add http://127.0.0.1:12391/`

LaunchAgent (clarioinc, no sudo): `scripts/webhook-listener.plist` template ->
`scripts/listener-install.sh` renders it to `~/Library/LaunchAgents/com.clario.bb-webhook-listener.plist`, bootstraps it, and checks `/health`;
`scripts/listener-uninstall.sh` removes it. stdout/stderr go to `logs/listener.out.log` / `logs/listener.err.log`.

### `scripts/send-from-tester.sh <recipient> <text>` — inbound sender
Sends an iMessage from **clarioinc's** Messages.app via AppleScript (tries the macOS 14+ `participant … of account` API, falls back to `buddy … of service`). Prints `T0=<ISO ms>`, `T0_MS=<epoch ms>`, `API=<participant|buddy>`. First run will trigger a macOS Automation permission prompt (Terminal -> Messages); allow it.

### `scripts/tests/` — acceptance tests
All source `common.sh` (env loading, `make_label`, `wait_for_event`, `count_events_by_text`, `count_events_by_guid`, `duplicate_guids`, `latency_ms`, `assert_count`, `result`). Labels look like `T-<NAME>-<epoch>-<rand>` so each run is unique and greppable. Each test prints `PASS`/`FAIL` and exits non-zero on failure.

| Script | Guide step | What it does |
|---|---|---|
| `t13-existing-chat.sh` | 13 | finds the 1:1 chat with `TEST_RECIPIENT`, sends a labeled message via BlueBubbles, waits for the outbound echo webhook, asserts exactly one message with the label in the chat and exactly one `new-message` event for its GUID; prints `isFromMe`/handle |
| `t14-new-chat.sh <address>` | 14 | `chat/new` with a labeled message; asserts chat exists (with participants) and exactly one message; on HTTP 500 / AppleScript error prints **NEW-CHAT FAILURE — see guide step 25** |
| `t15-inbound.sh [--label NAME]` | 15 | tester sends a labeled message to `SENDER01_EMAIL`; waits for the `new-message` webhook; prints T0, `dateCreated` (Messages.app arrival), `receivedAt` (detection), latencies; asserts exactly one event and no duplicate GUIDs |
| `t16-idle.sh [--minutes 30,30,60]` | 16 | sleeps 30 min -> `IDLE-30`, 30 more -> `IDLE-60`, 60 more -> `IDLE-120` (each an inbound test); no duplicates allowed; writes `logs/idle-report.md`. `--minutes 1,2,3` for a dry run. Do not run under `caffeinate` (step 17: baseline first). |
| `t18-wifi.sh --yes` | 18 | Wi-Fi `en1` off, 20 s, tester sends `WIFI-DOWN` (recorded only), Wi-Fi on, waits for `ping 1.1.1.1` and `bb.sh ping`, runs inbound `WIFI-RECOVERY`, asserts single delivery and zero duplicate GUIDs. **The default route is on en1 — run from a local terminal, never over SSH.** Refuses without `--yes`. |
| `run-all.sh [new-chat-address]` | 13-15 | runs t13, t14, t15 and prints a summary table (`logs/run-all.<ts>.results`) |
| `post-reboot-check.sh [--run-tests] [addr]` | 21, 37 | checklist: internet, imsg01 console session, imsg01 Messages.app, BlueBubbles process, port 12341, `bb.sh ping/info`, webhook registered, listener health, LaunchAgent loaded, tester Messages.app; `--run-tests` then runs 13/14/15 only if health is OK |

Steps 19 (restart BlueBubbles) and 20 (`killall Messages; open -a Messages` inside imsg01) are manual; re-verify with `run-all.sh` afterwards.

### `scripts/imsg01/` — apply later, inside imsg01's own session (steps 22-23, only if idle baseline fails)
| Script | Step | What |
|---|---|---|
| `app-sleep-disable.sh` | 22 | `defaults write com.apple.iChat NSAppSleepDisabled -bool YES` + read-back (refuses unless `whoami` = imsg01) |
| `messages-keepalive.sh` | 23 | every 300 s: `pgrep -x Messages`; if absent `open -gja Messages` (background, no focus); logs to `~/Library/Logs/messages-keepalive.log` |
| `com.imsg.messages-keepalive.plist` | 23 | LaunchAgent template (`StartInterval` 300) |
| `keepalive-install.sh` / `keepalive-uninstall.sh` | 23 | copy script to `~/Library/Application Support/imsg-keepalive/`, render + bootstrap the agent / remove it |

Copy `scripts/imsg01/` somewhere imsg01 can read (e.g. `/Users/Shared/imsg01-tools`) before switching users.

## Run order

1. **Step 12 — configure sender 1.** In imsg01's session set the BlueBubbles server password to the value of `BB_PASSWORD` in `.env`, keep it bound to `127.0.0.1:12341`, no proxy/ngrok. Back in clarioinc: fill `SENDER01_EMAIL` and `TEST_RECIPIENT` in `.env`, then
   `scripts/bb.sh ping && scripts/bb.sh info`.
2. **Listener.** `scripts/listener-install.sh`, then `scripts/bb.sh webhook-add http://127.0.0.1:12391/` and confirm with `scripts/bb.sh webhooks`. `curl 127.0.0.1:12391/health`.
3. **Steps 13-15.** `scripts/tests/run-all.sh` (or the three scripts individually). Step 13 needs an existing 1:1 chat with `TEST_RECIPIENT` (from step 9); step 14 defaults its address to `TEST_RECIPIENT` — pass a second consenting handle to test a truly new chat.
4. **Step 16.** `scripts/tests/t16-idle.sh` from a local terminal / tmux; leave the Mac alone; read `logs/idle-report.md`. (Step 17: no keepalive yet.)
5. **Step 18.** `scripts/tests/t18-wifi.sh --yes` from a local terminal.
6. **Steps 19-20.** Restart BlueBubbles / Messages inside imsg01, then `scripts/tests/run-all.sh`.
7. **Step 21.** Reboot; log imsg01 in; `scripts/tests/post-reboot-check.sh --run-tests`.
8. **Steps 22-23 (only if idle tests failed).** In imsg01: `app-sleep-disable.sh`, re-login, rerun `t16-idle.sh`; if still failing, `keepalive-install.sh`, rerun `t16-idle.sh`.
9. **Step 24-27 gate.** All green -> freeze config. New-chat-only failure (t14) -> step 25 note. Broad instability -> step 26.

## Files

```
.env.example                          committed template
.env                                  gitignored; BB_PASSWORD pre-generated
logs/                                 gitignored; events.jsonl, listener logs, idle-report.md, run-all results
scripts/bb.sh
scripts/webhook-listener.mjs
scripts/webhook-listener.plist
scripts/listener-install.sh
scripts/listener-uninstall.sh
scripts/send-from-tester.sh
scripts/tests/common.sh
scripts/tests/t13-existing-chat.sh
scripts/tests/t14-new-chat.sh
scripts/tests/t15-inbound.sh
scripts/tests/t16-idle.sh
scripts/tests/t18-wifi.sh
scripts/tests/run-all.sh
scripts/tests/post-reboot-check.sh
scripts/imsg01/app-sleep-disable.sh
scripts/imsg01/messages-keepalive.sh
scripts/imsg01/com.imsg.messages-keepalive.plist
scripts/imsg01/keepalive-install.sh
scripts/imsg01/keepalive-uninstall.sh
docs/TOOLKIT.md
```
