# Pilot pass/fail criteria

Deployment guide step 38: *define pass/fail criteria before launch.* Written 2026-09-15,
before any real outreach. Agreeing these afterwards is how a failing pilot gets argued
into looking successful.

Fill in the business targets marked **[decide]** before the first real send.

---

## 1. Hard stops

If any of these happens even once, **pause every sender immediately** and investigate
before another message goes out. These are not thresholds to tune; they mean something
is broken in a way that damages recipients or the accounts.

| # | Condition | Why it is a stop |
|---|---|---|
| S1 | The same recipient receives the same message twice | The duplicate-send failure the whole reconcile design exists to prevent. If it fires, the safeguard has failed. |
| S2 | A message is sent to a suppressed or opted-out address | Legal exposure, and the one promise made to recipients. |
| S3 | A message is sent from a sender that is not the contact's sticky sender | Breaks identity continuity; the recipient sees a stranger continuing a thread. |
| S4 | An Apple account is locked, rate-limited or challenged | Early warning of platform enforcement. Do not "wait and see". |
| S5 | A message is sent outside consent (no valid consent record) | Same as S2. |

Every one of these is currently prevented by a database constraint or the pre-send gate.
A stop therefore means a safeguard failed, not merely that a rule was broken.

## 2. Reliability gates

Measured over the pilot window. Failing one of these means fix before scaling, not stop.

| # | Metric | Pass | Notes |
|---|---|---|---|
| R1 | Unreconciled `failed` messages | **0** | A `failed` row with `reconciled = false` means we genuinely do not know whether it delivered. Non-zero means the reconcile fallback is not good enough. |
| R2 | Delivery rate of accepted sends | **≥ 95%** | Excludes `error=22` unreachable addresses, which are a list-quality problem (see Q1). |
| R3 | Inbound latency, send to recorded | **< 60 s p95** | Measured ~0.7-2.6 s in testing. A regression means the listener or webhook path is degrading. |
| R4 | Manual interventions | **≤ 1 per week** | Anything a human had to do: restart, re-login, clear a stuck dialog. This is the number that decides whether 5 senders is viable. |
| R5 | Unplanned sender downtime | **< 1%** | The health checker flips `senders.status`; measure from that. |
| R6 | Duplicate inbound/echo events *rejected* | tracked, not capped | Expected to be high (8 of 11 in testing). Rejection is correct behaviour; a sudden drop to zero suggests the dedupe path stopped running. |

## 3. List quality gates

| # | Metric | Pass | Notes |
|---|---|---|---|
| Q1 | Unreachable addresses (`error=22`) | **< 10%** | Each costs ~90 s of worker time before failing. Above 10% the list is wrong for iMessage, not the system. Prefer phone numbers. |
| Q2 | New-conversation success rate | **≥ 90%** | First contact is the risky path and the one that times out by design. |

## 4. Business gates — **[decide]**

These are yours; the system cannot judge them.

| # | Metric | Target | Notes |
|---|---|---|---|
| B1 | Reply rate | **[decide]** % | Compare against your existing email control. |
| B2 | Opt-out rate | **[decide]** %, suggest a ceiling of 2% | A high opt-out rate is the clearest signal the targeting or copy is wrong. Treat breaching this as a stop, not a gate. |
| B3 | Cost per qualified conversation | **[decide]** | Must beat the control channel, or the pilot failed even if everything worked. |
| B4 | Qualified conversations | **[decide]** count | Absolute volume, not just rate. |

## 5. Scale gate

Do **not** add senders 3-5 until all of the following hold:

1. No hard stop (section 1) has occurred.
2. Every reliability gate (section 2) passes.
3. Sender 2 has run the guide's 48-hour foreground/background test with no material
   degradation in the background session (guide step 30).
4. Business gates (section 4) are met or the user explicitly accepts them as not met.
5. Counsel has signed off on consent, sender identification and opt-out handling.

## 6. Reporting

Run per sender and in total:

- counts by `messages.state`
- `failed` where `reconciled = false` (must be 0 — R1)
- duplicate `provider_guid` rejections
- suppressions created, split by `reason`
- inbound count and latency distribution
- cap refusals by type (once caps ship) — persistent `cap_daily` refusals mean demand
  exceeds safe capacity and the answer is more senders, not a higher cap

## 7. Stop-the-pilot conditions

Beyond the hard stops, end the pilot if:

- opt-out rate exceeds the B2 ceiling
- any recipient complains about consent
- Apple restricts any account
- manual interventions exceed R4 for two consecutive weeks — that is a sign the
  architecture needs work before it carries more senders
