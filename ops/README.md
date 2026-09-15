# Mac-side services

These run on the Mac mini. Together they make sending automatic: nothing here
needs a human once the machine is powered on.

| Service | Runs as | Starts | Purpose |
|---|---|---|---|
| `com.clario.bridge-worker` | LaunchDaemon (boot, no login) | always | Claims queued messages and sends them via BlueBubbles |
| `com.clario.bridge-health` | LaunchDaemon (boot, no login) | always | Flips `senders.status` when a BlueBubbles instance dies or recovers |
| `com.clario.bb-webhook-listener` | LaunchDaemon (boot, no login) | always | Receives BlueBubbles events, writes inbound messages |
| `com.imsg.launch-bluebubbles` | LaunchAgent in imsg01's GUI session | at imsg01 login | Starts BlueBubbles Server itself |

BlueBubbles is the only one that needs a GUI session, because Messages.app does.
Auto-login for `imsg01` is enabled (`sysadminctl -autologin`, FileVault off), so that
session exists after a reboot without anyone signing in.

All four use `KeepAlive`, so a crash is restarted automatically. Verified by killing
the worker and watching launchd bring it back with a new pid.

## Install / reinstall

    sudo cp ops/<label>.plist /Library/LaunchDaemons/
    sudo chown root:wheel /Library/LaunchDaemons/<label>.plist
    sudo chmod 644 /Library/LaunchDaemons/<label>.plist
    sudo launchctl bootout system/<label> 2>/dev/null
    sudo launchctl bootstrap system /Library/LaunchDaemons/<label>.plist

## Check

    sudo launchctl print system/com.clario.bridge-worker | grep state
    tail -f logs/com.clario.bridge-worker.out

## Stop sending without uninstalling

Pause the sender row instead of killing the worker - the worker will skip it and
nothing is lost:

    update senders set status='paused', paused_reason='operator' where slug='sender01';
