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
| 30/60/120 idle tests [16] | **PASS** (latency, losses, wake) / duplicates per Defect B |
| Wi-Fi recovery [18] | **PASS** |
| BlueBubbles restart [19] | PASS (incidental) |
| Messages restart [20] | PASS (incidental) |
| Mac reboot/recovery [21] | **PASS with caveat** (see below) |
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

## Idle reliability [16]

Ran 2026-09-10 21:51Z to 23:52Z, machine untouched throughout. Outbound mode
(inbound generation is unavailable, see Note 2).

| label | gap | sent | err | echo latency | duplicate |
|---|---|---|---|---|---|
| IDLE-30 | T+30m | yes | 0 | 747 ms | yes |
| IDLE-60 | T+60m | yes | 0 | 742 ms | yes |
| IDLE-120 | T+120m | yes | 0 | 922 ms | yes |

No losses, no manual wake-up, no restart, latency under one second throughout. PASS.

### Extended idle: 71 hours

Re-tested 2026-09-13 09:51Z after the Mac sat untouched since 2026-09-10 14:29Z.
BlueBubbles was still the SAME process (pid 42961, no restart in 71 h), Messages still
running, API healthy. A live send returned HTTP 200 with echo latency 723 ms.
This is a stronger result than the guide's 120-minute requirement.

## Wi-Fi interruption and recovery [18]

Ran 2026-09-13 18:19Z on en1 (the default route). Script re-enables Wi-Fi via an exit trap
on every path: `scripts/tests/t18-wifi-run.sh`.

| Step | Result |
|---|---|
| Wi-Fi off, default route gone | confirmed |
| Send attempted while offline | accepted (HTTP 200), queued by Messages |
| Wi-Fi on, network restored | ~4 s |
| Offline message delivered after recovery | yes, `error=0`, delivered 2 s after link returned |
| Recovery send | yes, `error=0`, delivered |
| Message lost | none |
| Duplicate **message** | none (1 distinct GUID per message) |
| Manual Messages restart required | no |
| Manual BlueBubbles restart required | no (same pid 42961 throughout) |

PASS. Recovery is fully automatic. Note the queue-then-deliver behaviour: a send issued
while offline still returns HTTP 200 and is delivered later, so "accepted" never means
"delivered". The bridge must treat delivery confirmation, not the API response, as truth.

Duplicate *events* occurred on both sends, consistent with Defect B.

## Reboot recovery [21]

Real reboot 2026-09-13 11:25 local (shutdown 11:24, confirmed in `last reboot`).

### What did NOT come back on its own

For 87 minutes after boot the sender was completely offline:

| Component | State after boot, before any login |
|---|---|
| imsg01 session | not logged in (no loginwindow process) |
| Messages under imsg01 | not running |
| BlueBubbles | not running |
| Port 12341 | dead |
| API | no response |
| Tailscale / Screen Sharing | up (system daemons, unaffected) |
| Webhook listener | up only because it is a clarioinc LaunchAgent |

Cause: **no auto-login is configured.** The Mac boots to a login window and stops.
The guide's runbook [37] assumes a human logs into each sender account after boot.

### After logging into imsg01 (12:52)

| Check | Result |
|---|---|
| BlueBubbles auto-started with the session | yes, 12:52:42, no manual launch |
| Port 12341 listening | yes |
| API ping | 200 |
| Webhook registration survived reboot | yes, id 1 intact |
| Messages identity | intact, no re-auth needed |
| Post-reboot send | HTTP 200, `error=0`, delivered |
| Echo latency | 2620 ms (first send after cold start; steady state ~700 ms) |
| Duplicate message | none (1 distinct GUID) |

So recovery is clean *once a session exists*. The caveat is that a session does not
create itself.

### Recommendation

Enable auto-login for imsg01 (FileVault is off, so it is available; needs admin once).
Without it, any restart - power cut, macOS update, crash - silently takes the sender
offline until a human notices. That is a standing outage risk at one sender and an
unacceptable one at five. If auto-login is rejected on security grounds, a boot-time
alert is mandatory instead.

## Recipient reachability and send-path performance (2026-09-15)

Sender identity: meidy@clariocapital.com (an attempted swap to chilldaddygerry@gmail.com left the
account signed in but never IDS-registered - empty ActiveAccounts/OnlineAccounts and a permanently
spinning address list. Cleared by quitting Messages and removing the account plists; backups in
imsg01's `~/diag/`. Treat a spinning "You can be reached at" list as a hard failure, not a delay.)

| Recipient | Delivered | Notes |
|---|---|---|
| meidy765@gmail.com | yes, `error=0` | valid two-party test |
| +17783231234 | yes, `error=0` | valid two-party test |
| meidy@clariocapital.com | yes, `error=0` | **loopback** - same account as the sender, so it produces both an outbound AND an inbound row. Useless as a test target. |
| gerryvienlifeflores@gmail.com | no, `error=22` | not registered with Apple |
| chilldaddygerry@gmail.com | no, `error=22` | not registered with Apple |
| thisyourman106@gmail.com | no, `error=22` | not registered with Apple |

### The two send paths perform very differently

| Path | Latency | API response |
|---|---|---|
| `POST /message/text` into an existing chat | **0.76 s** | clean HTTP 200 |
| `POST /chat/new` | **75-120 s, always times out** | timeout, but the message DELIVERS |

`chat/new` delivers reliably but never returns in time. This is Defect A at its worst.

**Design consequence:** the worker must create a conversation ONCE via `chat/new`, tolerate the
timeout, reconcile the result, then use `message/text` with the resulting chat GUID for every
subsequent send. Treating `chat/new` as the normal send path would cap throughput at roughly one
message every two minutes and generate a timeout on every single send.

Unreachable addresses also hang ~90-100 s before failing, so reachability must be pre-checked
rather than discovered per message, or one bad address stalls everything behind it.

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
with an identical GUID. Verified server-side: a single BlueBubbles process and a single
registered webhook.

Final rate across all traffic: **7 of 10 outbound duplicated, 0 of 2 inbound**.
Every successful `POST /api/v1/message/text` into an existing chat duplicated (7 of 7).
The three outbound messages that did not duplicate were a native Messages send, a send
that returned 500 (Defect A), and a `chat/new` send. So the trigger is a *successful*
`message/text` send, which is precisely the hot path the bridge will use.

Mitigation, already required by the guide [31, 35]: unique index on `provider_guid` and
dedupe every inbound/echo event by GUID. This defect is the reason that rule exists.

## Note 2 — Inbound generation is unavailable

clarioinc's Terminal is denied Apple Events access to Messages (-1743) and the permission
prompt will not display, so inbound test traffic cannot be generated automatically.
Inbound itself is PROVEN (two real replies received and webhooked, neither duplicated);
only unattended *generation* is blocked. Grant manually via System Settings >
Privacy & Security > Automation if continuous inbound testing is wanted.

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

**All Phase 1 functional gates now pass.** Existing-chat send, new-chat send, inbound,
idle 30/60/120 (plus 71 h extended), Wi-Fi recovery, and reboot recovery are green.

Two conditions must be met before sender 2 [28]:

1. **Auto-login for imsg01**, or an equivalent boot-time alert. Reboot leaves the sender
   dead until a human logs in. Unresolved.
2. **Defects A and B must be handled in the bridge** before any production traffic:
   reconcile before retry, and a unique index on provider_guid. These are guide
   requirements [31/33/35], not optional hardening.

Defect B in particular is not a rare edge case: 8 of 11 outbound sends duplicated.
Defects A and B are handled in the bridge layer, not blockers to sender 1 itself.
Do NOT proceed to sender 2 [28] until the idle, Wi-Fi and reboot items are green.
