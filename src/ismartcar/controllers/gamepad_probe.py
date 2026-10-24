#!/usr/bin/env python3
"""Read-only discovery and input test for Bluetooth/USB gamepads.

No GPIO, PWM, motor, or servo commands are issued by this program.
Install with: sudo apt install python3-evdev
Use: sudo python3 gamepad_probe.py --list
     sudo python3 gamepad_probe.py --device /dev/input/eventX
"""

import argparse

from evdev import InputDevice, ecodes, list_devices


_GAMEPAD_BUTTONS = {
    ecodes.BTN_SOUTH,
    ecodes.BTN_EAST,
    ecodes.BTN_NORTH,
    ecodes.BTN_WEST,
    ecodes.BTN_START,
    ecodes.BTN_SELECT,
    ecodes.BTN_THUMBL,
    ecodes.BTN_THUMBR,
}


def is_likely_gamepad(device: InputDevice) -> bool:
    keys = set(device.capabilities().get(ecodes.EV_KEY, []))
    name = device.name.lower()
    return bool(keys & _GAMEPAD_BUTTONS) or any(
        word in name for word in ("gamepad", "joystick", "controller", "xbox")
    )


def enumerate_devices() -> list[tuple[str, str, bool]]:
    result = []
    for path in list_devices():
        device = InputDevice(path)
        try:
            result.append((device.path, device.name, is_likely_gamepad(device)))
        finally:
            device.close()
    return sorted(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List Linux input devices")
    parser.add_argument("--device", metavar="PATH", help="Monitor one /dev/input/eventX")
    args = parser.parse_args()

    devices = enumerate_devices()
    if args.list or not args.device:
        if not devices:
            print("No accessible input devices found.")
        for path, name, candidate in devices:
            tag = "GAMEPAD?" if candidate else "other"
            print(f"{path:22} [{tag:8}] {name}")
        if not args.device:
            print("Choose a gamepad event path and run with --device /dev/input/eventX")
            return

    if not args.device.startswith("/dev/input/event"):
        parser.error("--device must be a /dev/input/eventX path")

    device = InputDevice(args.device)
    try:
        print(f"Monitoring: {device.name} ({device.path})")
        print("Move sticks and press buttons. Press Ctrl+C to stop.")
        for event in device.read_loop():
            if event.type not in (ecodes.EV_KEY, ecodes.EV_ABS):
                continue
            code_name = ecodes.bytype[event.type].get(event.code, str(event.code))
            print(f"{ecodes.EV[event.type]} {code_name}: {event.value}", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        device.close()


if __name__ == "__main__":
    main()
