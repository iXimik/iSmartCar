#!/usr/bin/env python3
"""Independent 5-minute health check for the G910 Bluetooth connection.

Run as root from ismartcar-gamepad-watchdog.timer (systemd). Unlike the
auto-connect daemon, this script uses the HCI connection table as a second
source of truth. It never removes pairings, touches GPIO/PWM, or controls motors.

It takes action ONLY when:
  1. The auto-connect daemon's loop heartbeat has stopped for >150 seconds,
     in which case systemd restarts that daemon; or
  2. BlueZ says G910 is disconnected, but the ASUS controller still shows a
     stale ACL link in TWO checks separated by four seconds. It then performs
     the targeted 'btmgmt ... disconnect' tested on this Raspberry Pi; or
  3. BlueZ reports a connected G910 but the ASUS adapter shows NO ACL link
     in TWO checks separated by four seconds. It requests the device's
     BlueZ Device1.Disconnect() to clear the phantom connected state.

Missing tools, unexpected adapter IDs, and uncertain states are log-only.
Never force a Bluetooth disconnect if BlueZ and HCI both show connected.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

from bluetooth_autoconnect import (
    ASUS_BLUETOOTH_MAC,
    G910_MAC,
    HEARTBEAT_FILE,
    find_objects,
    log,
)

SERVICE = "ismartcar-gamepad.service"
MAX_HEARTBEAT_AGE = 150
CONFIRM_DELAY = 4


def command(args: list[str], *, timeout: int = 12) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def recover_daemon(dry_run: bool) -> None:
    """Restart only if the auto-connect process is down or its loop is hung."""
    check = command(["systemctl", "is-active", SERVICE])
    active = check.returncode == 0 and check.stdout.strip() == "active"
    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    except FileNotFoundError:
        age = float("inf")
    except OSError as exc:
        log(f"WATCHDOG: heartbeat cannot be checked ({exc}); no process action")
        return

    if active and age <= MAX_HEARTBEAT_AGE:
        log(f"WATCHDOG: reconnect service healthy, heartbeat age {age:.0f}s")
        return

    reason = "inactive" if not active else f"stalled heartbeat ({age:.0f}s)"
    log(f"WATCHDOG: reconnect service {reason}; restarting with systemd")
    if dry_run:
        log("DRY-RUN: systemctl restart " + SERVICE)
        return

    result = command(["systemctl", "restart", SERVICE], timeout=25)
    if result.returncode:
        log(f"WATCHDOG: restart failed: {(result.stderr or result.stdout).strip()}")
    else:
        log("WATCHDOG: reconnect service restarted")


def hci_name(adapter_path: str) -> str | None:
    # Discover adapter by ASUS MAC via BlueZ first; do not assume hci1 forever.
    match = re.fullmatch(r"/org/bluez/(hci[0-9]+)", adapter_path)
    return match.group(1) if match else None


def has_acl(hci: str) -> bool | None:
    try:
        result = command(["hcitool", "-i", hci, "con"], timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"WATCHDOG: cannot read ACL connections: {exc}")
        return None
    if result.returncode:
        log(f"WATCHDOG: hcitool failed: {(result.stderr or result.stdout).strip()}")
        return None
    return bool(re.search(r"<\s+ACL\s+" + re.escape(G910_MAC) + r"\b", result.stdout, re.I))


def bluez_status(bus) -> tuple[str | None, bool | None, bool | None]:
    adapter_path, _, props = find_objects(bus)
    if not adapter_path:
        return None, None, None
    if not props or not bool(props.get("Paired", False)):
        # Never disconnect an unpaired or unknown device.
        return adapter_path, None, None
    return (
        adapter_path,
        bool(props.get("Connected", False)),
        bool(props.get("ServicesResolved", False)),
    )


def clear_stale_acl(bus, dry_run: bool) -> None:
    """Reconcile BlueZ and kernel HCI states without disturbing healthy links."""
    first_path, first_connected, first_resolved = bluez_status(bus)
    if first_path is None:
        log(f"WATCHDOG: ASUS adapter {ASUS_BLUETOOTH_MAC} missing")
        return
    hci = hci_name(first_path)
    if not hci:
        log(f"WATCHDOG: unexpected ASUS BlueZ adapter path {first_path}; skipped")
        return

    first_acl = has_acl(hci)
    if first_connected is None or first_acl is None:
        log("WATCHDOG: cannot safely compare G910 state, no cleanup")
        return
    if first_connected == first_acl:
        log(f"WATCHDOG: G910 BlueZ connected={first_connected}, "
            f"HID resolved={first_resolved}, HCI ACL={first_acl}; consistent")
        return

    # Both mismatch directions occur with this specific G910 and USB-BT400:
    #   BlueZ=False, ACL=True  -> stale kernel ACL handle
    #   BlueZ=True, ACL=False  -> phantom BlueZ connected/HID state
    phantom_bluez = first_connected and not first_acl
    kind = "phantom BlueZ connection" if phantom_bluez else "stale HCI ACL"
    log(f"WATCHDOG: suspicious G910 state ({kind}) on {hci}; confirming")
    time.sleep(CONFIRM_DELAY)

    # Recheck *both* sources; a transition here is a normal reconnect and
    # must not be interrupted. A four-second confirmation alone does not
    # prove all mismatches are stale, but avoids resetting transient states.
    second_path, second_connected, _ = bluez_status(bus)
    second_acl = has_acl(hci) if second_path == first_path else None
    if second_connected is not first_connected or second_acl is not first_acl:
        log("WATCHDOG: connection state changed during verification; leave it alone")
        return

    if phantom_bluez:
        # Disconnect via the *actual* paired Device1 on the ASUS adapter,
        # without removing the bond or touching the other Bluetooth adapter.
        current_adapter, device_path, props = find_objects(bus)
        if (current_adapter != first_path or not device_path or not props
                or not bool(props.get("Paired", False))
                or not bool(props.get("Connected", False))):
            log("WATCHDOG: G910 state changed before BlueZ cleanup; skipped")
            return
        if has_acl(hci) is not False:
            log("WATCHDOG: ACL changed before BlueZ cleanup; skipped")
            return

        log(f"WATCHDOG: confirmed phantom BlueZ connection for {G910_MAC}")
        if dry_run:
            log(f"DRY-RUN: BlueZ Device1.Disconnect() on {device_path}")
            return

        try:
            import dbus
            device = dbus.Interface(bus.get_object("org.bluez", device_path),
                                    "org.bluez.Device1")
            device.Disconnect(timeout=12)
        except dbus.exceptions.DBusException as exc:
            log(f"WATCHDOG: BlueZ disconnect failed: {exc}")
            return
        log("WATCHDOG: phantom BlueZ connection cleared; reconnect service will retry")
        return

    log(f"WATCHDOG: confirmed stale ACL for {G910_MAC} on {hci}; disconnecting")
    if dry_run:
        log(f"DRY-RUN: btmgmt -i {hci} disconnect -t public {G910_MAC}")
        return

    # The other mismatch direction: kernel still retains an ACL handle,
    # but BlueZ does not consider this gamepad connected.
    try:
        result = command(
            ["btmgmt", "-i", hci, "disconnect", "-t", "public", G910_MAC],
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"WATCHDOG: targeted disconnect could not run: {exc}")
        return
    if result.returncode:
        log(f"WATCHDOG: disconnect failed: {(result.stderr or result.stdout).strip()}")
        return

    log("WATCHDOG: stale ACL released; existing pairing preserved")
    remaining = has_acl(hci)
    if remaining is True:
        log("WATCHDOG: ACL appears again; reconnect daemon may be retrying")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log actions without restarting service or disconnecting ACL",
    )
    args = parser.parse_args()
    log("WATCHDOG: five-minute Bluetooth check started")
    # Order matters: clean old ACL first, then ensure the reconnect loop runs.
    try:
        import dbus
        clear_stale_acl(dbus.SystemBus(), args.dry_run)
    except (ImportError, RuntimeError, OSError) as exc:
        log(f"WATCHDOG: BlueZ check unavailable: {exc}")
    except Exception as exc:
        # Avoid blocking service recovery when D-Bus is temporarily unavailable.
        log(f"WATCHDOG: BlueZ check failed ({type(exc).__name__}): {exc}")

    try:
        recover_daemon(args.dry_run)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"WATCHDOG: systemd check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
