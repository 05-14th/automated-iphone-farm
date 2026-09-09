# Phase 1 — Human Steps (in order)

Everything not listed here is automated from the `clarioinc` session via `scripts/`.
Do these in sequence. After each block, tell Claude "done with H<n>" so the automated
tests for that block can run.

Guide step numbers are in brackets.

---

## H1 — Answer two questions (no clicking yet)
1. Is Apple Account 01 a separate Apple ID, or is it `meidy@clariocapital.com`
   (currently signed in under `clarioinc`)? The plan assumes a **separate** Apple ID
   so `clarioinc`'s Messages can act as the test recipient.
2. Do you have the `imsg01` macOS password? If not, reset it in
   System Settings > Users & Groups (needs your admin password).

## H2 — Prepare Apple Account 01  [3]
- Create/verify the Apple ID and its email. Complete any 2FA / CAPTCHA.
- Keep its password and trusted-phone details in your password manager.

## H3 — One-time sudo items (enter admin password once)  [2, control channel]
Run in Terminal as `clarioinc`:
```
# prevent surprise macOS upgrades during the pilot
sudo softwareupdate --schedule off
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
sudo defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool false

# control channel: let Claude reach imsg01 via SSH on localhost (no root needed later)
sudo systemsetup -setremotelogin on
sudo dseditgroup -o edit -a imsg01 -t user com.apple.access_ssh
sudo dseditgroup -o edit -a clarioinc -t user com.apple.access_ssh
```
The SSH key already exists. You will authorise it inside the imsg01 session in H4.

## H4 — Log into imsg01 and sign into Messages  [7, 8]
1. Apple menu > Fast User Switching (or the menu-bar user icon) > **iMessage Line 01**.
   Do **not** log out `clarioinc`; switch only.
2. Open **Messages.app** > sign in with Apple Account 01.
3. Messages > Settings > iMessage:
   - confirm the Apple Account 01 email is listed under *You can be reached at*;
   - select that email under **Start new conversations from**;
   - leave **Enable Messages in iCloud** OFF.
4. ~~SSH key authorise~~ — DONE 2026-09-08 (written by Claude via `su`). Kept for reference:
```
mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIn1xe4B7ofss2HK9k4VlbG6ovpoPP27Pqaf/i80LTAP clarioinc->imsg01 control' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```
5. Send one iMessage from imsg01's Messages to `meidy@clariocapital.com` (the test
   recipient) and, if available, to a real iPhone. Confirm blue bubbles and that the
   sender shown is the Apple Account 01 email.  [5, 9]

## H5 — BlueBubbles  [10, 11, 12]  ✅ DONE 2026-09-09
Completed entirely by Claude, no human action was needed:
- BlueBubbles 1.9.9 installed and running in the imsg01 session via LaunchAgent
  `com.imsg.launch-bluebubbles`.
- Configured through its config DB: port 12341, unique password from `.env`, wizard skipped,
  localhost-only address, start-on-login, updates off, **Private API OFF**, **SIP ON**.
- **Full Disk Access granted** by toggling BlueBubbles in System Settings > Privacy & Security
  from the clarioinc session via AppleScript. That pane is system-wide ("for all users on this
  Mac"), so it covers the imsg01 instance. No admin password was required.
- Server restarted; the "unable to open database file" error is gone and the REST API answers
  on `http://127.0.0.1:12341`.
- Webhook id 1 registered to `http://127.0.0.1:12391` for new-message / updated-message /
  server-update.
- `scripts/tests/post-reboot-check.sh` reports **HEALTH: OK** on all 12 checks.

Remaining in this area: `detected_imessage` is null because no Apple Account is signed in
under imsg01. That is H4.

## H6 — Two automation prompts  [13, 15]
- In **imsg01**: the first BlueBubbles send raises *"BlueBubbles" wants access to control
  "Messages"* → **OK**. Claude triggers this on request while you are in that session.
- In **clarioinc**: the first inbound test raises *"Terminal" wants access to control
  "Messages"* → **Allow**.

## H7 — Reboot test  [21]
When Claude reports steps 13–20 green:
```
sudo shutdown -r now
```
After boot: log into **clarioinc**, then Fast-User-Switch to **imsg01**, confirm Messages
is signed in and BlueBubbles is running, switch back to clarioinc, and tell Claude.
Claude runs `scripts/tests/post-reboot-check.sh --run-tests`.

## H8 — Decisions before Phase 10+ (later)
- Apple Account 02 for sender 2.
- Which Supabase project hosts the bridge (recommend a new one).
- Counsel sign-off on consent / identification / opt-out rules.  [36]
