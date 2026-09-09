# Phase-1 host state

Captured: 2026-09-06 14:13:24 PDT (2026-09-06T21:13:24Z)
Host: Clarios-Mac-mini.local   User: clarioinc

## sw_vers

```
ProductName:		macOS
ProductVersion:		26.3
BuildVersion:		25D125
```

## uname -m

```
arm64
```

## csrutil status

```
System Integrity Protection status: enabled.
```

## pmset -g custom

```
AC Power:
 Sleep On Power Button 1
 lowpowermode         0
 standby              0
 ttyskeepawake        1
 powernap             0
 displaysleep         0
 womp                 1
 networkoversleep     0
 sleep                0
 tcpkeepalive         1
 autorestart          1
 disksleep            0
 SleepServices        0
```

## Hardware (system_profiler SPHardwareDataType)

```
Model Name: Mac mini
Model Identifier: Mac16,10
Chip: Apple M4
Memory: 16 GB
Serial Number (system): YVWVGC0XJL
```

## BlueBubbles installed

```
Path: /Applications/BlueBubbles.app
Version: 1.9.9
Bundle ID: com.BlueBubbles.BlueBubbles-Server
Quarantine xattr: none
--- codesign -dv --verbose=2
Executable=/Applications/BlueBubbles.app/Contents/MacOS/BlueBubbles
Identifier=com.BlueBubbles.BlueBubbles-Server
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 size=782 flags=0x10000(runtime) hashes=13+7 location=embedded
Signature size=9048
Authority=Developer ID Application: Zachary Shames (WPV275H8W7)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=May 16, 2025 at 12:23:56 PM
Info.plist entries=33
TeamIdentifier=WPV275H8W7
Runtime Version=13.3.0
Sealed Resources version=2 rules=13 files=67
Internal requirements count=1 size=196
--- codesign --verify --deep --strict
signature valid
--- spctl -a -vv
/Applications/BlueBubbles.app: rejected
source=Unnotarized Developer ID
origin=Developer ID Application: Zachary Shames (WPV275H8W7)
(exit 3)
```

## DMG sha256

```
fafd650c883f52e7494a6625e45249f2144d197378a4d57143ccf6198bb2e862  /Users/clarioinc/automated-iphone-farm-phase1-setup/downloads/BlueBubbles-1.9.9-arm64.dmg
size: 300676880 bytes
```

## Users (dscl . list /Users, non-service)

```
clarioinc
daemon
imsg01
nobody
root
```

## imsg01

```
NFSHomeDirectory: /Users/imsg01
PrimaryGroupID: 20
RealName:
 iMessage  Line 01
UniqueID: 502
UserShell: /bin/zsh
admin group: no (Standard user)
```

## Listening ports 12341-12345 (lsof)

```
(nothing listening on 12341-12345)
```

## Default network interface (route -n get default)

```
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.254
  interface: en1
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0 
```

## Interface addresses

```
	ether 8a:0c:c1:91:36:a4
	inet 192.168.1.67 netmask 0xffffff00 broadcast 192.168.1.255
```

## Install provenance (Task A, 2026-09-06)

- Source: https://github.com/BlueBubblesApp/bluebubbles-server/releases/download/v1.9.9/BlueBubbles-1.9.9-arm64.dmg
- GitHub release API (`releases/latest`) reports tag `v1.9.9`, asset size 300676880 bytes; no `digest` field present in the asset JSON, so only size was compared (match).
- DMG sha256: `fafd650c883f52e7494a6625e45249f2144d197378a4d57143ccf6198bb2e862`
- Installed via `hdiutil attach -nobrowse -readonly` + `ditto` to `/Applications/BlueBubbles.app` (no sudo needed; /Applications is admin-group writable). App was not launched.
- `chmod -R o+rX` applied so the Standard user `imsg01` can execute it.
- Signature: Developer ID Application: Zachary Shames (WPV275H8W7), hardened runtime, `codesign --verify --deep --strict` passes.
- Gatekeeper: `spctl -a -vv` returns **rejected / Unnotarized Developer ID**. No `com.apple.quarantine` xattr is present (curl download), so Gatekeeper should not block the first launch; if it does, an admin can `spctl --add` / right-click Open once.
- Re-capture this file with `scripts/record-state.sh > docs/STATE.md`.

## 2026-09-09 — BlueBubbles operational

- BlueBubbles 1.9.9 running under imsg01 via LaunchAgent `com.imsg.launch-bluebubbles` (gui/502).
- Full Disk Access granted (system-wide pane, no admin auth prompt appeared).
- REST API live on http://127.0.0.1:12341; `server/info` reports server_version 1.9.9,
  os_version 26.3.0, private_api false, proxy_service dynamic-dns.
- Webhook id 1 -> http://127.0.0.1:12391 (new-message, updated-message, server-update).
- post-reboot-check.sh: HEALTH OK, 12/12.
- BLOCKED: `detected_imessage` and `detected_icloud` are null. No Apple Account is signed
  into Messages under imsg01, so no sender identity exists yet and steps 13-16 cannot run.

### Operating notes
- imsg01 is currently the foreground console session; clarioinc is switched to the background.
  Synthetic HID events (computer-use clicks/typing) do NOT reach a background session.
  AppleScript UI scripting via System Events does work against it.
- Run commands as imsg01 with `/Users/Shared/imsg-setup/run.sh <script-in-that-dir>` (expect + su).
