# Phase 1 Report — Email-Based BlueBubbles / iMessage Deployment

Generated 2026-09-10. Sender 1 only. Guide step numbers in brackets.

## Environment

| Item | Value |
|---|---|
| Hardware | Mac mini, Apple M4, 16 GB (Mac16,10) |
| macOS | 26.3 (25D125), arm64 |
| SIP | Enabled |
| BlueBubbles Server | 1.9.9 (Developer ID signed, not notarized) |
| Private API | OFF (`helper_connected: false`) |
| macOS user | imsg01 (Standard, uid 502), console session |
| Port / proxy | 12341, localhost only (`dynamic-dns` -> 127.0.0.1) |
| Webhook | id 1 -> http://127.0.0.1:12391 |
| Sender identity | meidy@clariocapital.com |
| Test recipient | meidy765@gmail.com (clarioinc's iMessage alias) |

## Checklist [Immediate Phase-1 Checklist]

| Item | Result |
|---|---|
| Apple Account 01 prepared, email verified | PASS |
| Email identity usable for iMessage | PASS (functionally; see Note 1) |
| Native Mac iMessage send/receive | PASS |
| imsg01 Standard user created | PASS |
| Messages signed in under imsg01 | PASS |
| Native Mac new-chat creation | PASS |
| BlueBubbles installed, permissions granted | PASS (FDA + Automation) |
| SIP ON, Private API OFF | PASS |
| Port 12341 + unique password | PASS |
| Existing-chat API send [13] | PASS delivery / FAIL reporting (Defect A) |
| Brand-new-chat API send [14] | PASS delivery / FAIL reporting (Defect A) |
| Inbound event [15] | PASS |
| 30/60/120 idle tests [16] | RUNNING |
| Wi-Fi recovery [18] | NOT RUN |
| BlueBubbles restart [19] | PASS (incidental) |
| Messages restart [20] | PASS (incidental) |
| Mac reboot/recovery [21] | NOT RUN |
| No unexpected duplicates | **FAIL (Defect B)** |

## Evidence

Messages exchanged through the stack, all `error=0`:

| Time | Chat | Direction | Text |
|---|---|---|---|
| 14:16:25 | meidy765@gmail.com | out | Test (native) |
| 14:16:33 | meidy765@gmail.com | in | Hello |
| 14:33:39 | meidy765@gmail.com | out | PERMTEST2 (API) |
| 14:33:46 | meidy765@gmail.com | in | Hi |
| 14:35:39 | meidy765@gmail.com | out | T-EXIST (API) [13] |
| 14:38:38 | +17783231234 | out | T-NEWCHAT (API, new chat) [14] |

## Defects

### Defect A — API reports failure on successful send
`POST /api/v1/message/text` and `/api/v1/chat/new` return HTTP 500 or time out at 30s
while the message is in fact delivered (`error=0`, present in provider state).
Root cause: the server's await/reconcile step gives up before AppleScript settles;
`chat/new` additionally throws `-1728 Can't get chat id` after creating the chat.

Impact: a naive worker that trusts the API response and retries WILL double-send.
Mitigation, already required by the guide [33]: on timeout or 5xx, reconcile against
provider state by `tempGuid`/text before any retry. Never retry blind.

### Defect B — Duplicate outbound echo webhooks
The server dispatches the same `new-message` event twice for one message, ~1 ms apart,
with an identical GUID. Observed on 3 of 6 outbound sends. Verified this is server-side:
a single BlueBubbles process and a single registered webhook.

Inbound messages did NOT duplicate in any observation.

Mitigation, already required by the guide [31, 35]: unique index on `provider_guid` and
dedupe every inbound/echo event by GUID. This defect is the reason that rule exists.

## Note 1 — Diagnostic traps found the hard way

- `detected_imessage` / `detected_icloud` in `server/info` are populated by the Private API
  helper. With Private API OFF they are always `null`. They are NOT a sign-in indicator.
- iCloud sign-in (`MobileMeAccounts`) is separate from iMessage sign-in
  (`com.apple.imservice.ids.iMessage`). imsg01 has the latter, not the former; that is fine.
- `IMD-IDS-Aliases` reads empty under imsg01 despite iMessage working correctly. Not reliable.
- `pgrep` inside an SSH session lists ALL users' processes. Confirm with `ps -o user,pid`.
- `launchctl kickstart -k` on a job whose program is `open -a` does NOT restart the app;
  it only re-activates the running instance, so a TCC reset appears to have no effect.
  Kill the process explicitly.
- TCC denials must be cleared with `tccutil reset AppleEvents <bundle-id>` AND the app
  restarted, or the cached denial persists in-process.

## Gate decision [24-27]

Not yet passable. Outstanding: idle series (running), Wi-Fi recovery [18], reboot recovery [21].
Defects A and B are handled in the bridge layer, not blockers to sender 1 itself.
Do NOT proceed to sender 2 [28] until the idle, Wi-Fi and reboot items are green.
