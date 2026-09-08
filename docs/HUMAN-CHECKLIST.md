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

## H5 — Grant BlueBubbles its permissions inside imsg01  [10, 11, 12]
STATUS 2026-09-08: BlueBubbles 1.9.9 is already launched in the imsg01 session and fully
configured by Claude via its config DB (port 12341, unique password, tutorial skipped,
Dynamic-DNS proxy pointed at http://127.0.0.1:12341, start-on-login, updates off,
Private API OFF, SIP ON). It is stuck at "unable to open database file" until it gets
Full Disk Access. Only the permission clicks remain:
1. In the imsg01 session open System Settings > Privacy & Security > **Full Disk Access**,
   click **+**, add `/Applications/BlueBubbles.app`, toggle it ON.
2. Same for **Accessibility** (needed for the AppleScript send path).
3. If a Gatekeeper dialog ever appears for BlueBubbles: Privacy & Security > **Open Anyway**.
4. Tell Claude — it relaunches BlueBubbles from the clarioinc session and verifies
   `bb.sh ping` on port 12341. Do not log out imsg01; just switch back.

Tell Claude "done with H5". Claude then runs steps 13–16 automatically
(existing chat, new chat, inbound, idle 30/60/120).

## H6 — One-click permission prompt in clarioinc  [15]
The first inbound test triggers a macOS prompt:
*"Terminal" wants access to control "Messages"* → click **Allow**.

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
