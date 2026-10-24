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
    # A successful method call does not guarantee that the HID link will stay
    # connected. The next poll checks Connected again.
    log("CONNECT: BlueZ accepted request; verifying connection on next poll")
    return "connected"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true",
        help="Try checking/connecting once and show the result, without scanning",
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

    # IMPORTANT: keep the same system-bus connection across BR/EDR discovery.
    # BlueZ ends discovery sessions if their owning D-Bus client disappears.
    bus = dbus.SystemBus()
    last_state: str | None = None
    failures = 0
    next_scan_after = 0.0
    last_missing_scan_at = -1e9

    try:
        while True:
            state = "unknown"
            attempted = False
            try:
                adapter_path, device_path, props = find_objects(bus)
                if adapter_path is None:
                    state = "no_adapter"
                    log("WAIT: ASUS USB-BT400 adapter is absent")
                elif not bool(
                    dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPERTIES)
                    .Get(ADAPTER_IFACE, "Powered", timeout=10)
                ):
                    # Power the chosen USB adapter even if BlueZ has no G910
                    # device record yet (cold boot or USB re-enumeration).
                    dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPERTIES).Set(
                        ADAPTER_IFACE, "Powered", dbus.Boolean(True), timeout=10
                    )
                    state = "powered_on"
                    log("POWER: ASUS USB-BT400 enabled")
                elif device_path is None:
                    state = "no_device"
                    if not args.once and (
                        time.monotonic() - last_missing_scan_at >= 40
                    ):
                        last_missing_scan_at = time.monotonic()
                        try:
                            scan_bredr(bus, adapter_path, args.scan_seconds)
                        except dbus.exceptions.DBusException as exc:
                            log(f"SCAN: failed: {exc}")
                    log("WAIT: paired G910 record not found on ASUS adapter; "
                        "check pairing via bluetoothctl")
                elif props is not None and bool(props.get("Connected", False)):
                    state = "connected"
                    failures = 0
                    next_scan_after = 0.0
                else:
                    # The user's paired controller may need several seconds
                    # after waking before it accepts a host-initiated link.
                    attempted = True
                    state = connect_once(bus)
                    if state == "connected":
                        failures = 0
                        next_scan_after = 0.0
                    elif state in ("powered_on", "not_paired"):
                        failures = 0
            except dbus.exceptions.DBusException as exc:
                state = "dbus_error"
                if attempted:
                    failures += 1
                log(f"RETRY: BlueZ error: {exc}")
            except (OSError, RuntimeError) as exc:
                state = "error"
                if attempted:
                    failures += 1
                log(f"RETRY: {exc}")

            if state != last_state:
                if state == "connected":
                    log("STATUS: gamepad connected to ASUS adapter")
                elif last_state == "connected":
                    log("STATUS: gamepad disconnected; reconnection enabled")
                last_state = state

            if args.once:
                return 0 if state == "connected" else 1

            # Several unsuccessful Connect calls can indicate that G910 is in
            # standby or has just woken up. A bounded BR/EDR inquiry helps
            # refresh discovery without resetting pairing or the USB adapter.
            if failures >= 2 and time.monotonic() >= next_scan_after:
                next_scan_after = time.monotonic() + 45.0
                failures = 0
                try:
                    target_adapter, _, _ = find_objects(bus)
                    if target_adapter is not None:
                        scan_bredr(bus, target_adapter, args.scan_seconds)
                except dbus.exceptions.DBusException as exc:
                    log(f"SCAN: retry skipped: {exc}")

            time.sleep(args.retry_seconds)
    except KeyboardInterrupt:
        log("Stopped by operator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
