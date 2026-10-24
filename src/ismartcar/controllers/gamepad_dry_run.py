#!/usr/bin/env python3
"""G910 dry-run controller: read input and print intended car actions.

SAFETY: This module never opens GPIO/I2C, controls motors, or moves servos.
Run:
    sudo python3 src/ismartcar/controllers/gamepad_dry_run.py
    sudo python3 src/ismartcar/controllers/gamepad_dry_run.py --device /dev/input/event2
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evdev import InputDevice, ecodes, list_devices

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "g910.json"


def find_device(name: str) -> str:
    """Find matching controller; event numbers can change after reboot."""
    candidates = []
    for path in list_devices():
        dev = InputDevice(path)
        try:
            candidates.append(f"{path}: {dev.name}")
            if dev.name == name:
                return path
        finally:
            dev.close()
    raise RuntimeError(
        f"Input device '{name}' not found. Available: "
        + (", ".join(candidates) if candidates else "none")
    )


def axis_percent(raw: int, minimum: int, maximum: int, deadzone: float, invert: bool) -> int:
    """Convert centered analog axis to -100..100, with a dead zone."""
    center = (minimum + maximum) / 2.0
    if raw >= center:
        value = (raw - center) / max(maximum - center, 1.0)
    else:
        value = (raw - center) / max(center - minimum, 1.0)
    if invert:
        value = -value
    value = max(-1.0, min(1.0, value))
    if abs(value) <= deadzone:
        return 0
    magnitude = (abs(value) - deadzone) / (1.0 - deadzone)
    return round((1 if value > 0 else -1) * magnitude * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Linux event device; omit for auto-detection")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    deadzone = float(cfg["deadzone"])
    if not 0 <= deadzone < 1:
        parser.error("deadzone must be in [0, 1)")

    axis_map = {
        getattr(ecodes, spec["event"]): (name, bool(spec["invert"]))
        for name, spec in cfg["axes"].items()
    }
    button_map = {
        getattr(ecodes, code): action for code, action in cfg["buttons"].items()
    }
    # Extra controls are displayed only: no actuator action assigned yet.
    aux_button_map = {
        getattr(ecodes, code): label
        for code, label in cfg.get("aux_buttons", {}).items()
    }
    hat_map = {
        getattr(ecodes, code): labels
        for code, labels in cfg.get("hat_axes", {}).items()
    }
    levels = cfg["speed_levels_percent"]
    level_index = 0
    state = {name: 0 for name in cfg["axes"]}

    path = args.device or find_device(cfg["device_name"])
    device = InputDevice(path)
    try:
        print(f"G910 dry-run: {device.name} at {path}", flush=True)
        print("NO motors, servos, GPIO or I2C are controlled.", flush=True)
        print("Move sticks, D-pad, A/B/X/Y, shoulders, START/SELECT; Ctrl+C to stop.", flush=True)
        print("D-pad and auxiliary buttons are diagnostic only.", flush=True)

        for event in device.read_loop():
            if event.type == ecodes.EV_ABS and event.code in axis_map:
                name, invert = axis_map[event.code]
                info = device.absinfo(event.code)
                lower, upper = (info.min, info.max) if info else (0, 255)
                percent = axis_percent(event.value, lower, upper, deadzone, invert)
                if state[name] == percent:
                    continue
                state[name] = percent
                print(
                    f"steering={state['steering']:+4d}% "
                    f"throttle={state['throttle']:+4d}% "
                    f"camera_pan={state['camera_pan']:+4d}% "
                    f"camera_tilt={state['camera_tilt']:+4d}%",
                    flush=True,
                )
            elif event.type == ecodes.EV_ABS and event.code in hat_map:
                direction = hat_map[event.code].get(str(event.value), f"raw={event.value}")
                axis = "X" if event.code == ecodes.ABS_HAT0X else "Y"
                print(f"D-PAD {axis}: {direction} (diagnostic only)", flush=True)
            elif event.type == ecodes.EV_KEY:
                # EV_KEY 2 is an auto-repeat while held, not another press.
                if event.value == 2:
                    continue
                action = button_map.get(event.code)
                if action and event.value == 1:
                    if action == "cycle_speed":
                        level_index = (level_index + 1) % len(levels)
                        print(
                            f"BUTTON: speed limit -> {levels[level_index]}% (dry-run)",
                            flush=True,
                        )
                    else:
                        print(f"BUTTON: {action} (dry-run)", flush=True)
                elif event.code in aux_button_map and event.value in (0, 1):
                    label = aux_button_map[event.code]
                    state_text = "pressed" if event.value == 1 else "released"
                    print(f"AUX {label}: {state_text} (diagnostic only)", flush=True)
    except KeyboardInterrupt:
        print("\nStopped; no hardware was activated.")
    except OSError as exc:
        print(f"Gamepad disconnected or input error: {exc}; no hardware was activated.")
    finally:
        device.close()


if __name__ == "__main__":
    main()
