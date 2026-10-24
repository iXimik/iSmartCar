#!/usr/bin/env python3
"""Reconnect a previously paired G910 using the ASUS USB-BT400 only.

This uses BlueZ D-Bus (not a fragile bluetoothctl interactive session).
Never touches GPIO, PCA9685, motors, servos, or control commands.

Manual check:
    sudo /usr/bin/python3 bluetooth_autoconnect.py --once
Service:
    /usr/bin/python3 -u bluetooth_autoconnect.py
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import dbus
except ImportError:
    sys.exit("Missing D-Bus bindings. Install: sudo apt install python3-dbus")

BLUEZ = "org.bluez"
MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

# MAC addresses verified on this iSmartCar Raspberry Pi.
ASUS_BLUETOOTH_MAC = "5C:F3:70:7F:E0:D4"
G910_MAC = "05:67:39:43:89:01"


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def find_objects(bus: dbus.SystemBus) -> tuple[str | None, str | None, dict | None]:
    """Find the adapter by its MAC, not unstable hci0/hci1 numbering."""
    root = bus.get_object(BLUEZ, "/")
    objects = dbus.Interface(root, MANAGER).GetManagedObjects(timeout=10)

    adapter_path = next((
        str(path) for path, interfaces in objects.items()
        if ADAPTER_IFACE in interfaces
        and str(interfaces[ADAPTER_IFACE].get("Address", "")).upper()
        == ASUS_BLUETOOTH_MAC
    ), None)
    if adapter_path is None:
        return None, None, None

    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE_IFACE)
        if props is None:
            continue
        if (
            str(props.get("Address", "")).upper() == G910_MAC
            and str(props.get("Adapter", "")) == adapter_path
        ):
            return adapter_path, str(path), props
    return adapter_path, None, None


def scan_bredr(bus: dbus.SystemBus, adapter_path: str, seconds: float = 7.0) -> None:
    """Briefly scan for the classic-Bluetooth gamepad after failed reconnects.

    The D-Bus connection is kept alive for the entire discovery session.
    This uses a *separate scan session* and never removes bond/trust data.
    """
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), ADAPTER_IFACE)
    started = False
    try:
        adapter.SetDiscoveryFilter(
            dbus.Dictionary({"Transport": dbus.String("bredr")}, signature="sv"),
            timeout=10,
        )
        log(f"SCAN: BR/EDR inquiry for {seconds:g}s via ASUS USB-BT400")
        adapter.StartDiscovery(timeout=10)
        started = True
        time.sleep(seconds)
    finally:
        if started:
            try:
                adapter.StopDiscovery(timeout=10)
                log("SCAN: finished, continuing normal reconnection")
            except dbus.exceptions.DBusException as exc:
                log(f"SCAN: unable to end our discovery session: {exc}")
        try:
            adapter.SetDiscoveryFilter(dbus.Dictionary({}, signature="sv"), timeout=10)
        except dbus.exceptions.DBusException:
            pass


def connect_once(bus: dbus.SystemBus) -> str:
    adapter_path, device_path, properties = find_objects(bus)
    if adapter_path is None:
        log(f"WAIT: ASUS USB-BT400 ({ASUS_BLUETOOTH_MAC}) not found; retrying")
        return "no_adapter"

    adapter = bus.get_object(BLUEZ, adapter_path)
    adapter_properties = dbus.Interface(adapter, PROPERTIES)
    if not bool(adapter_properties.Get(ADAPTER_IFACE, "Powered", timeout=10)):
        log(f"POWER: enabling USB adapter {adapter_path}")
        adapter_properties.Set(
            ADAPTER_IFACE, "Powered", dbus.Boolean(True), timeout=10
        )
        # BlueZ can need a moment to repopulate devices after power-up.
        return "powered_on"

    if device_path is None:
        log(
            f"WAIT: G910 ({G910_MAC}) not present under ASUS adapter. "
            "Pair it with this adapter once using bluetoothctl."
        )
        return "no_device"

    assert properties is not None
    if not bool(properties.get("Paired", False)):
        log("WAIT: G910 exists but is not paired with the ASUS adapter")
        return "not_paired"

    if bool(properties.get("Connected", False)):
        return "connected"

    device = bus.get_object(BLUEZ, device_path)
    device_properties = dbus.Interface(device, PROPERTIES)
    if not bool(properties.get("Trusted", False)):
        # The user previously trusted this exact paired device.
        log("TRUST: marking already paired G910 trusted")
        device_properties.Set(
            DEVICE_IFACE, "Trusted", dbus.Boolean(True), timeout=10
        )

    log(f"CONNECT: attempting {G910_MAC} via {ASUS_BLUETOOTH_MAC}")
    dbus.Interface(device, DEVICE_IFACE).Connect(timeout=20)
    # Success here only means BlueZ accepted Connect(), not that a usable
    # input device was established or the link will stay up.
    log("CONNECT: BlueZ accepted request; awaiting HID service resolution")
    return "connecting"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true",
        help="Try connection once and verify BlueZ state, without scanning",
    )
    parser.add_argument(
        "--retry-seconds", type=float, default=8,
        help="Seconds between checks/reconnection attempts (default: 8)",
    )
    parser.add_argument(
        "--scan-seconds", type=float, default=7,
        help="Duration of occasional BR/EDR discovery (default: 7 seconds)",
    )
    args = parser.parse_args()
    if args.retry_seconds < 3 or not 2 <= args.scan_seconds <= 15:
        parser.error("retry-seconds >= 3 and 2 <= scan-seconds <= 15 required")

    log(
        f"iSmartCar Bluetooth auto-connect ready: ASUS {ASUS_BLUETOOTH_MAC}, "
        f"G910 {G910_MAC}. NO motor control."
    )

    # A persistent system bus is required by BlueZ discovery sessions.
    bus = dbus.SystemBus()
    last_state: str | None = None
    failure_count = 0
    connected_since: float | None = None
    pending_connect = False
    next_scan_after = 0.0
    last_missing_scan_at = -1e9
    stable_seconds = 20.0

    try:
        while True:
            state = "unknown"
            now = time.monotonic()
            attempted = False
            adapter_path = None
            try:
                adapter_path, device_path, props = find_objects(bus)
                if adapter_path is None:
                    state = "no_adapter"
                    log("WAIT: ASUS USB-BT400 adapter is absent")
                    connected_since = None
                    pending_connect = False
                elif not bool(
                    dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPERTIES)
                    .Get(ADAPTER_IFACE, "Powered", timeout=10)
                ):
                    dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPERTIES).Set(
                        ADAPTER_IFACE, "Powered", dbus.Boolean(True), timeout=10
                    )
                    state = "powered_on"
                    connected_since = None
                    pending_connect = False
                    log("POWER: ASUS USB-BT400 enabled")
                elif device_path is None:
                    state = "no_device"
                    connected_since = None
                    pending_connect = False
                    if not args.once and now - last_missing_scan_at >= 40:
                        last_missing_scan_at = now
                        try:
                            scan_bredr(bus, adapter_path, args.scan_seconds)
                        except dbus.exceptions.DBusException as exc:
                            log(f"SCAN: failed: {exc}")
                    log("WAIT: paired G910 record not found on ASUS adapter")
                elif not bool(props.get("Paired", False)):
                    state = "not_paired"
                    connected_since = None
                    pending_connect = False
                    log("WAIT: G910 is not paired with ASUS USB-BT400")
                elif bool(props.get("Connected", False)):
                    # A successful Connect() reply is NOT a confirmed HID link.
                    # Re-check BlueZ Device1 on the following poll.
                    if connected_since is None:
                        connected_since = now
                        log("LINK: Bluetooth transport connected; checking HID")
                    pending_connect = False
                    if bool(props.get("ServicesResolved", False)):
                        state = "connected"
                        if now - connected_since >= stable_seconds:
                            if failure_count:
                                log("LINK: HID connection stable; clearing retry count")
                            failure_count = 0
                            next_scan_after = 0.0
                    else:
                        state = "resolving"
                else:
                    # Count a failed connection even if Connect() originally
                    # returned successfully: the controller might have
                    # disconnected before our next poll.
                    if pending_connect:
                        failure_count += 1
                        pending_connect = False
                        log(f"LINK: previous connect did not hold "
                            f"(failures={failure_count})")
                    if connected_since is not None:
                        duration = max(0.0, now - connected_since)
                        if duration < stable_seconds:
                            failure_count += 1
                            log(f"LINK: short-lived connection (~{duration:.0f}s); "
                                f"failures={failure_count}")
                        connected_since = None

                    # Discover BEFORE attempting another connection, so our
                    # BR/EDR inquiry won't interfere with a new connect.
                    if not args.once and failure_count >= 2 and now >= next_scan_after:
                        next_scan_after = now + 45.0
                        failure_count = 0
                        state = "scanning"
                        scan_bredr(bus, adapter_path, args.scan_seconds)
                    else:
                        attempted = True
                        state = connect_once(bus)
                        pending_connect = state == "connecting"

                if state != last_state:
                    if state == "connected":
                        log("STATUS: BlueZ connected and HID services resolved")
                    elif last_state in ("connected", "resolving"):
                        log(f"STATUS: previous HID connection lost (state={state})")
                    last_state = state

                if args.once:
                    if state == "connecting":
                        # Give BlueZ time to resolve HID after Connect().
                        time.sleep(3)
                        _, _, check = find_objects(bus)
                        if check and bool(check.get("Connected", False)) and bool(
                            check.get("ServicesResolved", False)
                        ):
                            log("STATUS: BlueZ connected and HID services resolved")
                            return 0
                        log("STATUS: Connect() returned, but HID was not verified")
                        return 1
                    return 0 if state == "connected" else 1

            except dbus.exceptions.DBusException as exc:
                last_state = "dbus_error"
                if attempted:
                    failure_count += 1
                log(f"RETRY: BlueZ D-Bus error: {exc}")
            except (OSError, RuntimeError) as exc:
                last_state = "error"
                if attempted:
                    failure_count += 1
                log(f"RETRY: {exc}")

            time.sleep(args.retry_seconds)
    except KeyboardInterrupt:
        log("Stopped by operator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
