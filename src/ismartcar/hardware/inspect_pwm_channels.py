#!/usr/bin/env python3
"""Read-only PCA9685 channel inspection for original SunFounder wiring.

No register writes: this program cannot move servos or motors.
Run: python3 /home/pi/iSmartCar/src/ismartcar/hardware/inspect_pwm_channels.py
"""

from pca9685 import PCA9685

CHANNELS = {
    0: "steering servo",
    4: "left motor PWM enable",
    5: "right motor PWM enable",
    14: "camera pan servo",
    15: "camera tilt servo",
}


def inspect_channel(pwm: PCA9685, channel: int) -> dict[str, int | bool]:
    base = pwm.LED0_ON_L + 4 * channel
    on_low = pwm.read_register(base)
    on_high = pwm.read_register(base + 1)
    off_low = pwm.read_register(base + 2)
    off_high = pwm.read_register(base + 3)
    return {
        "on": on_low | ((on_high & 0x0F) << 8),
        "off": off_low | ((off_high & 0x0F) << 8),
        "full_on": bool(on_high & 0x10),
        "full_off": bool(off_high & 0x10),
    }


def main() -> None:
    with PCA9685() as pwm:
        state = pwm.status()
        print(
            "MODE1=0x{mode1:02X} MODE2=0x{mode2:02X} "
            "PRE_SCALE=0x{prescale:02X} SLEEP={sleeping}".format(**state)
        )
        for ch, label in CHANNELS.items():
            values = inspect_channel(pwm, ch)
            print(
                f"CH{ch:02d} {label:24s} "
                f"ON={values['on']:4d} OFF={values['off']:4d} "
                f"FULL_ON={values['full_on']} FULL_OFF={values['full_off']}"
            )
    print("Read-only inspection finished. No hardware output changed.")


if __name__ == "__main__":
    main()
