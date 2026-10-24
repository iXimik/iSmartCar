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
    log("CONNECTED: G910 via ASUS USB-BT400")
    return "connected"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true",
        help="Perform one status/connection attempt and exit (for diagnostics)",
    )
    parser.add_argument(
        "--retry-seconds", type=float, default=8,
        help="Seconds between checks/reconnection attempts (default: 8)",
    )
    args = parser.parse_args()
    if args.retry_seconds < 3:
        parser.error("--retry-seconds must be >= 3")

    log(
        f"iSmartCar Bluetooth auto-connect ready: ASUS {ASUS_BLUETOOTH_MAC}, "
        f"G910 {G910_MAC}. NO motor control."
    )
    last_state = None
    try:
        while True:
            try:
                bus = dbus.SystemBus()
                state = connect_once(bus)
            except dbus.exceptions.DBusException as exc:
                state = "dbus_error"
                log(f"RETRY: BlueZ D-Bus error: {exc}")
            except (OSError, RuntimeError) as exc:
                state = "error"
                log(f"RETRY: {exc}")
            else:
                if state != last_state:
                    if state == "connected" and last_state != "connected":
                        log("STATUS: gamepad connected")
                    elif last_state == "connected":
                        log("STATUS: gamepad disconnected, reconnecting")
            last_state = state
            if args.once:
                return 0 if state == "connected" else 1
            time.sleep(args.retry_seconds)
    except KeyboardInterrupt:
        log("Stopped by operator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
