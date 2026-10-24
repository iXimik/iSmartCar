#!/usr/bin/env python3
"""First steering-servo smoke test for iSmartCar / SunFounder PCA9685.

Default invocation: preview only, no I2C writes.
Physical steering test: --arm (ONLY when power is stable and chassis is secured).

Never starts traction motors or accesses GPIO direction pins.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time

from pca9685 import PCA9685


STEERING_CHANNEL = 0
LEGACY_CENTER_COUNT = 450
LEGACY_FREQUENCY_HZ = 60
MAX_HOLD_SECONDS = 0.5


def get_throttle_flags() -> int:
    result = subprocess.run(
        ["vcgencmd", "get_throttled"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    match = re.fullmatch(r"throttled=(0x[0-9a-fA-F]+)", result.stdout.strip())
    if match is None:
        raise RuntimeError(f"Unexpected vcgencmd output: {result.stdout!r}")
    return int(match.group(1), 16)


def require_stable_power() -> None:
    flags = get_throttle_flags()
    if flags & 0x5:
        raise RuntimeError(
            f"Current Pi undervoltage/throttling present (0x{flags:X}). "
            "Refusing servo activation. Fix 5V power supply/cable first."
        )
    if flags:
        print(f"NOTE: historic throttling flags exist (0x{flags:X}).", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm", action="store_true",
        help="Actually send a steering-centre servo pulse after safety checks",
    )
    args = parser.parse_args()

    print(f"Steering CH{STEERING_CHANNEL}, centre count {LEGACY_CENTER_COUNT}, "
          f"{LEGACY_FREQUENCY_HZ} Hz, max hold {MAX_HOLD_SECONDS}s.")
    if not args.arm:
        print("PREVIEW ONLY. No hardware registers changed.")
        print("Physical test requires --arm; currently undervoltage 0x50005 "
              "must be resolved before arming.")
        return

    # Two-stage protection: check power before opening I2C, then just before PWM.
    require_stable_power()
    with PCA9685() as pwm:
        state = pwm.status()
        if not state["sleeping"]:
            raise RuntimeError(
                "PCA9685 oscillator already active; refusing to interrupt "
                "another process controlling hardware."
            )

        # Refuse to wake the chip if another program left any channel enabled.
        for channel in range(16):
            off_h = pwm.read_register(pwm.LED0_ON_L + 4 * channel + 3)
            if not (off_h & 0x10):
                raise RuntimeError(
                    f"CH{channel} is not FULL_OFF; refusing to touch PWM outputs."
                )

        # Intentionally use the mechanical centre count from the original kit.
        # Chassis must be secured; even a centre command can move the servo.
        require_stable_power()
        try:
            pwm.all_outputs_off()
            pwm.set_frequency(LEGACY_FREQUENCY_HZ)
            pwm.write_pwm(STEERING_CHANNEL, 0, LEGACY_CENTER_COUNT)
            print("CH0 enabled briefly: steering should move toward its "
                  "original SunFounder centre.", flush=True)
            time.sleep(MAX_HOLD_SECONDS)
        finally:
            # Guaranteed attempt to stop PWM even on KeyboardInterrupt/error.
            try:
                pwm.all_outputs_off()
            finally:
                pwm.sleep()
        print("CH0 disabled, PCA9685 sleeping. Traction motors never enabled.")


if __name__ == "__main__":
    main()
