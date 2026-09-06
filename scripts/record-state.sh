#!/bin/bash
# record-state.sh — capture the Phase-1 host state to stdout. No sudo required.
# Usage: scripts/record-state.sh [> docs/state-$(date +%Y%m%d).txt]
set -u

APP="${BLUEBUBBLES_APP:-/Applications/BlueBubbles.app}"
DMG="${BLUEBUBBLES_DMG:-$(cd "$(dirname "$0")/.." && pwd)/downloads/BlueBubbles-1.9.9-arm64.dmg}"

section() { printf '\n## %s\n\n```\n' "$1"; }
endsection() { printf '```\n'; }
run() { "$@" 2>&1 || printf '(exit %s)\n' "$?"; }

echo "# Phase-1 host state"
echo
echo "Captured: $(date '+%Y-%m-%d %H:%M:%S %Z') ($(date -u '+%Y-%m-%dT%H:%M:%SZ'))"
echo "Host: $(hostname)   User: $(whoami)"

section "sw_vers";            run sw_vers;                                   endsection
section "uname -m";           run uname -m;                                  endsection
section "csrutil status";     run csrutil status;                            endsection
section "pmset -g custom";    run pmset -g custom;                           endsection
section "Hardware (system_profiler SPHardwareDataType)"
  run system_profiler SPHardwareDataType | grep -E 'Model Name|Model Identifier|Chip|Memory|Serial Number' | sed 's/^ *//'
endsection

section "BlueBubbles installed"
  if [ -d "$APP" ]; then
    echo "Path: $APP"
    echo "Version: $(defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString 2>&1)"
    echo "Bundle ID: $(defaults read "$APP/Contents/Info.plist" CFBundleIdentifier 2>&1)"
    echo "Quarantine xattr: $(xattr -p com.apple.quarantine "$APP" 2>/dev/null || echo none)"
    echo "--- codesign -dv --verbose=2"
    run codesign -dv --verbose=2 "$APP"
    echo "--- codesign --verify --deep --strict"
    codesign --verify --deep --strict "$APP" 2>&1 && echo "signature valid"
    echo "--- spctl -a -vv"
    run spctl -a -vv "$APP"
  else
    echo "NOT INSTALLED at $APP"
  fi
endsection

section "DMG sha256"
  if [ -f "$DMG" ]; then
    run shasum -a 256 "$DMG"
    echo "size: $(stat -f %z "$DMG") bytes"
  else
    echo "DMG not present at $DMG"
  fi
endsection

section "Users (dscl . list /Users, non-service)"; run dscl . list /Users | grep -v '^_'; endsection

section "imsg01"
  if dscl . read /Users/imsg01 >/dev/null 2>&1; then
    run dscl . read /Users/imsg01 RealName UniqueID PrimaryGroupID NFSHomeDirectory UserShell
    if dscl . read /Groups/admin GroupMembership 2>/dev/null | grep -qw imsg01; then
      echo "admin group: YES"
    else
      echo "admin group: no (Standard user)"
    fi
  else
    echo "user imsg01 does not exist"
  fi
endsection

section "Listening ports 12341-12345 (lsof)"
  out=$(lsof -nP -iTCP:12341-12345 -sTCP:LISTEN 2>/dev/null)
  if [ -n "$out" ]; then echo "$out"; else echo "(nothing listening on 12341-12345)"; fi
endsection

section "Default network interface (route -n get default)"; run route -n get default; endsection
section "Interface addresses"
  iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
  [ -n "$iface" ] && run ifconfig "$iface" | grep -E 'inet |ether' | sed 's/^ *//'
endsection
